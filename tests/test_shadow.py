from __future__ import annotations

import pytest
import torch
from qadence.backends.api import backend_factory
from qadence.blocks.abstract import AbstractBlock
from qadence.blocks.utils import add, chain, kron
from qadence.circuit import QuantumCircuit
from qadence.constructors import ising_hamiltonian, total_magnetization
from qadence.execution import expectation
from qadence.ml_tools.utils import rand_featureparameters
from qadence.model import QuantumModel
from qadence.operations import RX, RY, H, I, X, Y, Z
from qadence.parameters import Parameter
from qadence.types import BackendName, DiffMode
from torch import Tensor

from qadence_measurement.protocol import Measurements
from qadence_measurement.utils.data_acquisition import (
    _max_observable_weight,
    number_of_samples,
    shadow_samples,
)
from qadence_measurement.utils.post_processing import (
    expectation_estimations,
    local_shadow,
    robust_local_shadow,
)
from qadence_measurement.utils.unitaries import (
    P0_MATRIX,
    P1_MATRIX,
    UNITARY_TENSOR,
)
from qadence_measurement.utils.types import MeasurementProtocol
from qadence_measurement.utils.utils_trace import expectation_trace

idmat = torch.eye(2, dtype=torch.complex128)


@pytest.mark.parametrize(
    "observable, exp_weight",
    [
        (X(0), 1),
        (kron(*[X(0), Y(1), Z(2)]), 3),
        (add(*[X(0), Y(0), Z(0)]), 1),
        (kron(*[X(0), H(1), I(2), Z(3)]), 2),
        (total_magnetization(5), 1),
        (ising_hamiltonian(4), 2),
    ],
)
def test_weight(observable: AbstractBlock, exp_weight: int) -> None:
    qubit_weight = _max_observable_weight(observable)
    assert qubit_weight == exp_weight


@pytest.mark.parametrize(
    "observables, accuracy, confidence, exp_samples",
    [([total_magnetization(2)], 0.1, 0.1, (10200, 6))],
)
def test_number_of_samples(
    observables: list[AbstractBlock],
    accuracy: float,
    confidence: float,
    exp_samples: tuple,
) -> None:
    N, K = number_of_samples(observables=observables, accuracy=accuracy, confidence=confidence)
    assert N == exp_samples[0]
    assert K == exp_samples[1]


@pytest.mark.parametrize(
    "sample, unitary_ids, exp_shadow",
    [
        (
            torch.tensor([1, 0]),
            torch.tensor([0, 2]),
            torch.stack(
                [
                    3 * (UNITARY_TENSOR[0].adjoint() @ P1_MATRIX @ UNITARY_TENSOR[0]) - idmat,
                    3 * (UNITARY_TENSOR[2].adjoint() @ P0_MATRIX @ UNITARY_TENSOR[2]) - idmat,
                ]
            ),
        ),
        (
            torch.tensor([0, 1, 1, 1]),
            torch.tensor([2, 0, 2, 2]),
            torch.stack(
                [
                    3 * (UNITARY_TENSOR[2].adjoint() @ P0_MATRIX @ UNITARY_TENSOR[2]) - idmat,
                    3 * (UNITARY_TENSOR[0].adjoint() @ P1_MATRIX @ UNITARY_TENSOR[0]) - idmat,
                    3 * (UNITARY_TENSOR[2].adjoint() @ P1_MATRIX @ UNITARY_TENSOR[2]) - idmat,
                    3 * (UNITARY_TENSOR[2].adjoint() @ P1_MATRIX @ UNITARY_TENSOR[2]) - idmat,
                ]
            ),
        ),
    ],
)
def test_local_shadow(sample: Tensor, unitary_ids: list, exp_shadow: Tensor) -> None:
    shadow = local_shadow(bitstrings=sample, unitary_ids=unitary_ids)
    rshadow = robust_local_shadow(
        bitstrings=sample,
        unitary_ids=unitary_ids,
        calibration=torch.tensor([1.0 / 3.0] * len(sample)),
    )
    assert torch.allclose(shadow, exp_shadow)
    assert torch.allclose(rshadow, shadow)


@pytest.mark.flaky(max_runs=5)
@pytest.mark.parametrize(
    "circuit, observable, values",
    [
        (QuantumCircuit(1, X(0)), Z(0), {}),
        (QuantumCircuit(2, kron(X(0), X(1))), Z(0) @ Z(1), {}),
        (QuantumCircuit(2, kron(X(0), X(1))), X(0) @ X(1), {}),
        (QuantumCircuit(2, kron(X(0), X(1))), X(0) @ Y(1), {}),
        (QuantumCircuit(2, kron(X(0), X(1))), Y(0) @ X(1), {}),
        (QuantumCircuit(2, kron(X(0), X(1))), Y(0) @ Y(1), {}),
        (QuantumCircuit(2, kron(Z(0), H(1))), X(0) @ Z(1), {}),
        (
            QuantumCircuit(2, kron(RX(0, Parameter("theta")), X(1))),
            kron(Z(0), Z(1)),
            {"theta": torch.tensor([0.5, 1.0])},
        ),
        (QuantumCircuit(2, kron(X(0), Z(1))), ising_hamiltonian(2), {}),
    ],
)
def test_estimations_comparison_exact(
    circuit: QuantumCircuit, observable: AbstractBlock, values: dict
) -> None:
    backend = backend_factory(backend=BackendName.PYQTORCH, diff_mode=DiffMode.GPSR)
    (conv_circ, _, embed, params) = backend.convert(circuit=circuit, observable=observable)
    param_values = embed(params, values)
    exact_exp = expectation(circuit, observable, values=values)

    measurement_data = shadow_samples(shadow_size=5000, circuit=circuit, param_values=param_values)
    observables = [observable]
    K = number_of_samples(observables=observables, accuracy=0.1, confidence=0.1)[1]
    estimated_exp = expectation_estimations(
        observables=observables,
        unitaries_ids=measurement_data.unitaries,
        batch_shadow_samples=measurement_data.samples,
        K=K,
    )

    assert torch.allclose(estimated_exp, exact_exp, atol=0.2)


theta1 = Parameter("theta1", trainable=False)
theta2 = Parameter("theta2", trainable=False)
theta3 = Parameter("theta3", trainable=False)
theta4 = Parameter("theta4", trainable=False)


blocks = chain(
    kron(RX(0, theta1), RY(1, theta2)),
    kron(RX(0, theta3), RY(1, theta4)),
)
circuit = QuantumCircuit(2, blocks)


@pytest.mark.flaky(max_runs=5)
@pytest.mark.parametrize(
    "circuit, values, diff_mode",
    [
        (circuit, rand_featureparameters(circuit, 1), DiffMode.AD),
        (circuit, rand_featureparameters(circuit, 2), DiffMode.GPSR),
    ],
)
@pytest.mark.parametrize("do_kron", [True, False])
def test_estimations_comparison_tomo_forward_pass(
    circuit: QuantumCircuit,
    values: dict,
    diff_mode: DiffMode,
    do_kron: bool,
) -> None:
    observable = Z(0) ^ circuit.n_qubits if do_kron else X(1)  # type: ignore[operator]

    pyq_backend = backend_factory(BackendName.PYQTORCH, diff_mode=diff_mode)
    (conv_circ, conv_obs, embed, params) = pyq_backend.convert(circuit, observable)
    pyq_exp_exact = pyq_backend.expectation(conv_circ, conv_obs, embed(params, values))

    model = QuantumModel(
        circuit=circuit,
        observable=observable,
        backend=BackendName.PYQTORCH,
        diff_mode=DiffMode.GPSR,
    )

    options = {"n_shots": 100000}
    tomo_measurements = Measurements(protocol=MeasurementProtocol.TOMOGRAPHY, options=options)
    estimated_exp_tomo = tomo_measurements(model=model, param_values=values)

    shadow_options = {"accuracy": 0.1, "confidence": 0.1}
    shadow_measurements = Measurements(protocol=MeasurementProtocol.SHADOW, options=shadow_options)
    estimated_exp_shadow = shadow_measurements(model=model, param_values=values)

    n_shots = 1000

    N, K = number_of_samples([observable], **shadow_options)
    shadow_options2 = {"shadow_size": N // 10, "confidence": 0.1, "n_shots": n_shots}
    shadow_measurements2 = Measurements(
        protocol=MeasurementProtocol.SHADOW, options=shadow_options2
    )
    estimated_exp_shadow2 = shadow_measurements2(model=model, param_values=values)

    robust_options = {
        "shadow_size": N,
        "shadow_medians": K,
        "robust_correlations": None,
    }
    robust_shadows = Measurements(
        protocol=MeasurementProtocol.ROBUST_SHADOW,
        options=robust_options,
    )

    robust_options2 = {
        "shadow_size": shadow_options2["shadow_size"],
        "shadow_medians": K,
        "robust_correlations": None,
        "n_shots": n_shots,
    }
    robust_shadows2 = Measurements(
        protocol=MeasurementProtocol.ROBUST_SHADOW,
        options=robust_options2,
    )

    # set measurement same as classical shadows
    robust_shadows.data = shadow_measurements.data
    robust_estimated_exp_shadow = robust_shadows(
        model=model,
        param_values=values,
    )

    robust_shadows2.data = shadow_measurements2.data
    robust_estimated_exp_shadow2 = robust_shadows2(
        model=model,
        param_values=values,
    )

    assert torch.allclose(estimated_exp_tomo, pyq_exp_exact, atol=1.0e-2)
    assert torch.allclose(estimated_exp_shadow, pyq_exp_exact, atol=shadow_options["accuracy"])
    assert torch.allclose(
        robust_estimated_exp_shadow, pyq_exp_exact, atol=shadow_options["accuracy"]
    )
    assert torch.allclose(estimated_exp_shadow2, pyq_exp_exact, atol=shadow_options["accuracy"])
    assert torch.allclose(
        robust_estimated_exp_shadow2, pyq_exp_exact, atol=shadow_options["accuracy"]
    )

    # test expectation from reconstructed state
    state_snapshots_shadow = shadow_measurements.reconstruct_state()
    state_snapshots_rshadow = robust_shadows.reconstruct_state()

    state_snapshots_shadow2 = shadow_measurements2.reconstruct_state()
    state_snapshots_rshadow2 = robust_shadows2.reconstruct_state()

    exp_snapshots_shadow = expectation_trace(state_snapshots_shadow, observable)
    exp_snapshots_rshadow = expectation_trace(state_snapshots_rshadow, observable)

    assert torch.allclose(exp_snapshots_shadow, pyq_exp_exact, atol=shadow_options["accuracy"])
    assert torch.allclose(exp_snapshots_rshadow, pyq_exp_exact, atol=shadow_options["accuracy"])

    exp_snapshots_shadow2 = expectation_trace(state_snapshots_shadow2, observable)
    exp_snapshots_rshadow2 = expectation_trace(state_snapshots_rshadow2, observable)

    assert torch.allclose(exp_snapshots_shadow2, pyq_exp_exact, atol=shadow_options["accuracy"])
    assert torch.allclose(exp_snapshots_rshadow2, pyq_exp_exact, atol=shadow_options["accuracy"])


def test_shadow_raise_errors() -> None:
    # Bad input keys
    options = {"accuracy": 0.1, "conf": 0.1}
    with pytest.raises(KeyError):
        shadow_measurement = Measurements(
            protocol=MeasurementProtocol.SHADOW,
            options=options,
        )

    options = {"accuracies": 0.1, "confidence": 0.1}
    with pytest.raises(KeyError):
        shadow_measurement = Measurements(
            protocol=MeasurementProtocol.SHADOW,
            options=options,
        )
