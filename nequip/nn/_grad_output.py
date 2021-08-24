from typing import List, Union, Optional

import torch

from e3nn.o3 import Irreps
from e3nn.util.jit import compile_mode

from nequip.data import AtomicDataDict
from nequip.nn import GraphModuleMixin


@compile_mode("script")
class GradientOutput(GraphModuleMixin, torch.nn.Module):
    r"""Wrap a model and include as an output its gradient.

    Args:
        func: the model to wrap
        of: the name of the output field of ``func`` to take the gradient with respect to. The field must be a single scalar (i.e. have irreps ``0e``)
        wrt: the input field(s) of ``func`` to take the gradient of ``of`` with regards to.
        out_field: the field in which to return the computed gradients. Defaults to ``f"d({of})/d({wrt})"`` for each field in ``wrt``.
        sign: either 1 or -1; the returned gradient is multiplied by this.
    """
    sign: float
    _negate: bool

    def __init__(
        self,
        func: GraphModuleMixin,
        of: str,
        wrt: Union[str, List[str]],
        out_field: Optional[List[str]] = None,
        sign: float = 1.0,
    ):
        super().__init__()
        sign = float(sign)
        assert sign in (1.0, -1.0)
        self.sign = sign
        self._negate = sign == -1.0
        self.of = of
        # TO DO: maybe better to force using list?
        if isinstance(wrt, str):
            wrt = [wrt]
        if isinstance(out_field, str):
            out_field = [out_field]
        self.wrt = wrt
        self.func = func
        if out_field is None:
            self.out_field = [f"d({of})/d({e})" for e in self.wrt]
        else:
            assert len(out_field) == len(
                self.wrt
            ), "Out field names must be given for all w.r.t tensors"
            self.out_field = out_field

        # check and init irreps
        self._init_irreps(
            irreps_in=func.irreps_in,
            my_irreps_in={of: Irreps("0e")},
            irreps_out=func.irreps_out,
        )

        # The gradient of a single scalar w.r.t. something of a given shape and irrep just has that shape and irrep
        # Ex.: gradient of energy (0e) w.r.t. position vector (L=1) is also an L = 1 vector
        self.irreps_out.update(
            {f: self.irreps_in[wrt] for f, wrt in zip(self.out_field, self.wrt)}
        )

    def forward(self, data: AtomicDataDict.Type) -> AtomicDataDict.Type:
        # set req grad
        wrt_tensors = []
        old_requires_grad: List[bool] = []
        for k in self.wrt:
            old_requires_grad.append(data[k].requires_grad)
            data[k].requires_grad_(True)
            wrt_tensors.append(data[k])
        # run func
        data = self.func(data)
        # Get grads
        grads = torch.autograd.grad(
            # TODO:
            # This makes sense for scalar batch-level or batch-wise outputs, specifically because d(sum(batches))/d wrt = sum(d batch / d wrt) = d my_batch / d wrt
            # for a well-behaved example level like energy where d other_batch / d wrt is always zero. (In other words, the energy of example 1 in the batch is completely unaffect by changes in the position of atoms in another example.)
            # This should work for any gradient of energy, but could act suspiciously and unexpectedly for arbitrary gradient outputs, if they ever come up
            [data[self.of].sum()],
            wrt_tensors,
            create_graph=self.training,  # needed to allow gradients of this output during training
        )
        # return
        # grad is optional[tensor]?
        for out, grad in zip(self.out_field, grads):
            if grad is None:
                # From the docs: "If an output doesn’t require_grad, then the gradient can be None"
                raise RuntimeError("Something is wrong, gradient couldn't be computed")
            if self._negate:
                grad = torch.neg(grad)
            data[out] = grad

        # unset requires_grad_
        for req_grad, k in zip(old_requires_grad, self.wrt):
            data[k].requires_grad_(req_grad)

        return data


def ForceOutput(energy_model: GraphModuleMixin) -> GradientOutput:
    r"""Convinience constructor for ``GradientOutput`` with settings for forces.

    Args:
        energy_model: the model to wrap. Must have ``AtomicDataDict.TOTAL_ENERGY_KEY`` as an output.

    Returns:
        A ``GradientOutput`` wrapping ``energy_model``.
    """
    return GradientOutput(
        func=energy_model,
        of=AtomicDataDict.TOTAL_ENERGY_KEY,
        wrt=AtomicDataDict.POSITIONS_KEY,
        out_field=AtomicDataDict.FORCE_KEY,
        sign=-1,  # force is the negative gradient
    )


@compile_mode("script")
class StressOutput(GraphModuleMixin, torch.nn.Module):
    r"""Compute stress (and forces) using autograd of an energy model.

    Args:
        func: the model to wrap
    """
    do_forces: bool

    def __init__(self, energy_model: GraphModuleMixin, do_forces: bool = True):
        super().__init__()
        if not do_forces:
            raise NotImplementedError
        self.do_forces = do_forces

        # Displacement is to cell, so has same irreps?
        # TODO !!
        energy_model.irreps_in["_displacement"] = None

        self._grad = GradientOutput(
            func=energy_model,
            of=AtomicDataDict.TOTAL_ENERGY_KEY,
            wrt=[
                AtomicDataDict.POSITIONS_KEY,
                "_displacement",
            ],
            out_field=[
                AtomicDataDict.FORCE_KEY,
                AtomicDataDict.STRESS_KEY,
            ],
        )

        # check and init irreps
        irreps_in = self._grad.irreps_in.copy()
        assert AtomicDataDict.POSITIONS_KEY in irreps_in
        irreps_in[AtomicDataDict.CELL_KEY] = None  # three vectors
        del irreps_in["_displacement"]
        self._init_irreps(
            irreps_in=irreps_in,
            irreps_out=self._grad.irreps_out,
        )
        del self.irreps_out["_displacement"]

    def forward(self, data: AtomicDataDict.Type) -> AtomicDataDict.Type:
        # TODO: does any of this make sense without PBC? check it
        # Make the cell per-batch
        data = AtomicDataDict.with_batch(data)
        batch = data[AtomicDataDict.BATCH_KEY]
        num_batch: int = int(batch.max().cpu().item()) + 1
        data[AtomicDataDict.CELL_KEY] = (
            data[AtomicDataDict.CELL_KEY].view(-1, 3, 3).expand(num_batch, 3, 3)
        )
        # Add the displacements
        # the GradientOutput will make them require grad
        # See https://github.com/atomistic-machine-learning/schnetpack/blob/master/src/schnetpack/atomistic/model.py#L45
        displacement = torch.zeros(
            (num_batch, 3, 3),
            dtype=data[AtomicDataDict.CELL_KEY].dtype,
            device=data[AtomicDataDict.CELL_KEY].device,
        )
        displacement.requires_grad_(True)
        data["_displacement"] = displacement
        pos = data[AtomicDataDict.POSITIONS_KEY]
        # bmm is natom in batch
        data[AtomicDataDict.POSITIONS_KEY] = pos + torch.bmm(
            pos.unsqueeze(-2), displacement[batch]
        ).squeeze(-2)
        cell = data[AtomicDataDict.CELL_KEY]
        # bmm is num_batch in batch
        data[AtomicDataDict.CELL_KEY] = cell + torch.bmm(cell, displacement)

        # Call model and get gradients
        data = self._grad(data)

        # Put negative sign on forces
        data[AtomicDataDict.FORCE_KEY] = torch.neg(data[AtomicDataDict.FORCE_KEY])

        # Rescale stress tensor
        # See https://github.com/atomistic-machine-learning/schnetpack/blob/master/src/schnetpack/atomistic/output_modules.py#L180
        # First dim is batch, second is vec, third is xyz
        volume = torch.sum(
            cell[:, 0, :] * torch.cross(cell[:, 1, :], cell[:, 2, :], dim=1),
            dim=1,
            keepdim=True,
        )[..., None]
        assert len(volume) == num_batch
        data[AtomicDataDict.STRESS_KEY] = data[AtomicDataDict.STRESS_KEY] / volume

        # Remove helper
        del data["_displacement"]

        return data
