# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Test cases for the pulse scheduler passes."""

from numpy import pi
from qiskit import QuantumRegister, ClassicalRegister, QuantumCircuit, schedule
from qiskit.circuit import Gate, Parameter
from qiskit.circuit.library import U1Gate, U2Gate, U3Gate, SXGate
from qiskit.exceptions import QiskitError
from qiskit.pulse import (
    Schedule,
    DriveChannel,
    Delay,
    AcquireChannel,
    Acquire,
    MeasureChannel,
    MemorySlot,
    Gaussian,
    GaussianSquare,
    Play,
    transforms,
)
from qiskit.pulse import build, macros, play, InstructionScheduleMap

from qiskit.providers.fake_provider import FakeBackend, FakeOpenPulse2Q, FakeOpenPulse3Q, FakePerth
from qiskit.test import QiskitTestCase


class TestBasicSchedule(QiskitTestCase):
    """Scheduling tests."""

    def setUp(self):
        super().setUp()
        self.backend = FakeOpenPulse2Q()
        self.inst_map = self.backend.defaults().instruction_schedule_map

    def test_unavailable_defaults(self):
        """Test backend with unavailable defaults."""
        qr = QuantumRegister(1)
        qc = QuantumCircuit(qr)
        backend = FakeBackend(None)
        backend.defaults = backend.configuration
        self.assertRaises(QiskitError, lambda: schedule(qc, backend))

    def test_alap_pass(self):
        """Test ALAP scheduling."""

        #       ┌───────────────┐                    ░      ┌─┐
        # q0_0: ┤ U2(3.14,1.57) ├────────────────────░───■──┤M├───
        #       └┬──────────────┤ ░ ┌──────────────┐ ░ ┌─┴─┐└╥┘┌─┐
        # q0_1: ─┤ U2(0.5,0.25) ├─░─┤ U2(0.5,0.25) ├─░─┤ X ├─╫─┤M├
        #        └──────────────┘ ░ └──────────────┘ ░ └───┘ ║ └╥┘
        # c0: 2/═════════════════════════════════════════════╩══╩═
        #                                                    0  1
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U2Gate(3.14, 1.57), [q[0]])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        qc.barrier(q[1])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        qc.barrier(q[0], [q[1]])
        qc.cx(q[0], q[1])
        qc.measure(q, c)
        sched = schedule(qc, self.backend)
        # X pulse on q0 should end at the start of the CNOT
        expected = Schedule(
            (2, self.inst_map.get("u2", [0], 3.14, 1.57)),
            self.inst_map.get("u2", [1], 0.5, 0.25),
            (2, self.inst_map.get("u2", [1], 0.5, 0.25)),
            (4, self.inst_map.get("cx", [0, 1])),
            (26, self.inst_map.get("measure", [0, 1])),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_single_circuit_list_schedule(self):
        """Test that passing a single circuit list to schedule() returns a list."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        sched = schedule([qc], self.backend, method="alap")
        expected = Schedule()
        self.assertIsInstance(sched, list)
        self.assertEqual(sched[0].instructions, expected.instructions)

    def test_alap_with_barriers(self):
        """Test that ALAP respects barriers on new qubits."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(U2Gate(0, 0), [q[1]])
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule(
            self.inst_map.get("u2", [0], 0, 0), (2, self.inst_map.get("u2", [1], 0, 0))
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_empty_circuit_schedule(self):
        """Test empty circuit being scheduled."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule()
        self.assertEqual(sched.instructions, expected.instructions)

    def test_alap_aligns_end(self):
        """Test that ALAP always acts as though there is a final global barrier."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U3Gate(0, 0, 0), [q[0]])
        qc.append(U2Gate(0, 0), [q[1]])
        sched = schedule(qc, self.backend, method="alap")
        expected_sched = Schedule(
            (2, self.inst_map.get("u2", [1], 0, 0)), self.inst_map.get("u3", [0], 0, 0, 0)
        )
        for actual, expected in zip(sched.instructions, expected_sched.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])
        self.assertEqual(
            sched.ch_duration(DriveChannel(0)), expected_sched.ch_duration(DriveChannel(1))
        )

    def test_asap_pass(self):
        """Test ASAP scheduling."""

        #       ┌───────────────┐                    ░      ┌─┐
        # q0_0: ┤ U2(3.14,1.57) ├────────────────────░───■──┤M├───
        #       └┬──────────────┤ ░ ┌──────────────┐ ░ ┌─┴─┐└╥┘┌─┐
        # q0_1: ─┤ U2(0.5,0.25) ├─░─┤ U2(0.5,0.25) ├─░─┤ X ├─╫─┤M├
        #        └──────────────┘ ░ └──────────────┘ ░ └───┘ ║ └╥┘
        # c0: 2/═════════════════════════════════════════════╩══╩═
        #                                                    0  1
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U2Gate(3.14, 1.57), [q[0]])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        qc.barrier(q[1])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        qc.barrier(q[0], q[1])
        qc.cx(q[0], q[1])
        qc.measure(q, c)
        sched = schedule(qc, self.backend, method="as_soon_as_possible")
        # X pulse on q0 should start at t=0
        expected = Schedule(
            self.inst_map.get("u2", [0], 3.14, 1.57),
            self.inst_map.get("u2", [1], 0.5, 0.25),
            (2, self.inst_map.get("u2", [1], 0.5, 0.25)),
            (4, self.inst_map.get("cx", [0, 1])),
            (26, self.inst_map.get("measure", [0, 1])),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_alap_resource_respecting(self):
        """Test that the ALAP pass properly respects busy resources when backwards scheduling.
        For instance, a CX on 0 and 1 followed by an X on only 1 must respect both qubits'
        timeline."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        sched = schedule(qc, self.backend, method="as_late_as_possible")
        insts = sched.instructions
        self.assertEqual(insts[0][0], 0)
        self.assertEqual(insts[6][0], 22)

        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.append(U2Gate(0.5, 0.25), [q[1]])
        qc.measure(q, c)
        sched = schedule(qc, self.backend, method="as_late_as_possible")
        self.assertEqual(sched.instructions[-1][0], 24)

    def test_inst_map_schedules_unaltered(self):
        """Test that forward scheduling doesn't change relative timing with a command."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        sched1 = schedule(qc, self.backend, method="as_soon_as_possible")
        sched2 = schedule(qc, self.backend, method="as_late_as_possible")
        for asap, alap in zip(sched1.instructions, sched2.instructions):
            self.assertEqual(asap[0], alap[0])
            self.assertEqual(asap[1], alap[1])
        insts = sched1.instructions
        self.assertEqual(insts[0][0], 0)  # shift phase
        self.assertEqual(insts[1][0], 0)  # ym_d0
        self.assertEqual(insts[2][0], 0)  # x90p_d1
        self.assertEqual(insts[3][0], 2)  # cr90p_u0
        self.assertEqual(insts[4][0], 11)  # xp_d0
        self.assertEqual(insts[5][0], 13)  # cr90m_u0

    def test_measure_combined(self):
        """
        Test to check for measure on the same qubit which generated another measure schedule.

        The measures on different qubits are combined, but measures on the same qubit
        adds another measure to the schedule.
        """
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U2Gate(3.14, 1.57), [q[0]])
        qc.cx(q[0], q[1])
        qc.measure(q[0], c[0])
        qc.measure(q[1], c[1])
        qc.measure(q[1], c[1])
        sched = schedule(qc, self.backend, method="as_soon_as_possible")
        expected = Schedule(
            self.inst_map.get("u2", [0], 3.14, 1.57),
            (2, self.inst_map.get("cx", [0, 1])),
            (24, self.inst_map.get("measure", [0, 1])),
            (34, self.inst_map.get("measure", [0, 1]).filter(channels=[MeasureChannel(1)])),
            (34, Acquire(10, AcquireChannel(1), MemorySlot(1))),
        )
        self.assertEqual(sched.instructions, expected.instructions)

    def test_3q_schedule(self):
        """Test a schedule that was recommended by David McKay :D"""

        #                          ┌─────────────────┐
        # q0_0: ─────────■─────────┤ U3(3.14,1.57,0) ├────────────────────────
        #              ┌─┴─┐       └┬───────────────┬┘
        # q0_1: ───────┤ X ├────────┤ U2(3.14,1.57) ├───■─────────────────────
        #       ┌──────┴───┴──────┐ └───────────────┘ ┌─┴─┐┌─────────────────┐
        # q0_2: ┤ U2(0.778,0.122) ├───────────────────┤ X ├┤ U2(0.778,0.122) ├
        #       └─────────────────┘                   └───┘└─────────────────┘
        backend = FakeOpenPulse3Q()
        inst_map = backend.defaults().instruction_schedule_map
        q = QuantumRegister(3)
        c = ClassicalRegister(3)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.append(U2Gate(0.778, 0.122), [q[2]])
        qc.append(U3Gate(3.14, 1.57, 0), [q[0]])
        qc.append(U2Gate(3.14, 1.57), [q[1]])
        qc.cx(q[1], q[2])
        qc.append(U2Gate(0.778, 0.122), [q[2]])
        sched = schedule(qc, backend)
        expected = Schedule(
            inst_map.get("cx", [0, 1]),
            (22, inst_map.get("u2", [1], 3.14, 1.57)),
            (22, inst_map.get("u2", [2], 0.778, 0.122)),
            (24, inst_map.get("cx", [1, 2])),
            (44, inst_map.get("u3", [0], 3.14, 1.57, 0)),
            (46, inst_map.get("u2", [2], 0.778, 0.122)),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_schedule_multi(self):
        """Test scheduling multiple circuits at once."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc0 = QuantumCircuit(q, c)
        qc0.cx(q[0], q[1])
        qc1 = QuantumCircuit(q, c)
        qc1.cx(q[0], q[1])
        schedules = schedule([qc0, qc1], self.backend)
        expected_insts = schedule(qc0, self.backend).instructions
        for actual, expected in zip(schedules[0].instructions, expected_insts):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_circuit_name_kept(self):
        """Test that the new schedule gets its name from the circuit."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c, name="CIRCNAME")
        qc.cx(q[0], q[1])
        sched = schedule(qc, self.backend, method="asap")
        self.assertEqual(sched.name, qc.name)
        sched = schedule(qc, self.backend, method="alap")
        self.assertEqual(sched.name, qc.name)

    def test_can_add_gates_into_free_space(self):
        """The scheduler does some time bookkeeping to know when qubits are free to be
        scheduled. Make sure this works for qubits that are used in the future. This was
        a bug, uncovered by this example:

           q0 =  - - - - |X|
           q1 = |X| |u2| |X|

        In ALAP scheduling, the next operation on qubit 0 would be added at t=0 rather
        than immediately before the X gate.
        """
        qr = QuantumRegister(2)
        qc = QuantumCircuit(qr)
        for i in range(2):
            qc.append(U2Gate(0, 0), [qr[i]])
            qc.append(U1Gate(3.14), [qr[i]])
            qc.append(U2Gate(0, 0), [qr[i]])
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule(
            self.inst_map.get("u2", [0], 0, 0),
            self.inst_map.get("u2", [1], 0, 0),
            (2, self.inst_map.get("u1", [0], 3.14)),
            (2, self.inst_map.get("u1", [1], 3.14)),
            (2, self.inst_map.get("u2", [0], 0, 0)),
            (2, self.inst_map.get("u2", [1], 0, 0)),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_barriers_in_middle(self):
        """As a follow on to `test_can_add_gates_into_free_space`, similar issues
        arose for barriers, specifically.
        """
        qr = QuantumRegister(2)
        qc = QuantumCircuit(qr)
        for i in range(2):
            qc.append(U2Gate(0, 0), [qr[i]])
            qc.barrier(qr[i])
            qc.append(U1Gate(3.14), [qr[i]])
            qc.barrier(qr[i])
            qc.append(U2Gate(0, 0), [qr[i]])
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule(
            self.inst_map.get("u2", [0], 0, 0),
            self.inst_map.get("u2", [1], 0, 0),
            (2, self.inst_map.get("u1", [0], 3.14)),
            (2, self.inst_map.get("u1", [1], 3.14)),
            (2, self.inst_map.get("u2", [0], 0, 0)),
            (2, self.inst_map.get("u2", [1], 0, 0)),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_parametric_input(self):
        """Test that scheduling works with parametric pulses as input."""
        qr = QuantumRegister(1)
        qc = QuantumCircuit(qr)
        qc.append(Gate("gauss", 1, []), qargs=[qr[0]])
        custom_gauss = Schedule(
            Play(Gaussian(duration=25, sigma=4, amp=0.5, angle=pi / 2), DriveChannel(0))
        )
        self.inst_map.add("gauss", [0], custom_gauss)
        sched = schedule(qc, self.backend, inst_map=self.inst_map)
        self.assertEqual(sched.instructions[0], custom_gauss.instructions[0])

    def test_pulse_gates(self):
        """Test scheduling calibrated pulse gates."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(U2Gate(0, 0), [q[1]])
        qc.add_calibration("u2", [0], Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(0))), [0, 0])
        qc.add_calibration("u2", [1], Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(1))), [0, 0])

        sched = schedule(qc, self.backend)
        expected = Schedule(
            Play(Gaussian(28, 0.2, 4), DriveChannel(0)),
            (28, Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(1)))),
        )
        self.assertEqual(sched.instructions, expected.instructions)

    def test_calibrated_measurements(self):
        """Test scheduling calibrated measurements."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.measure(q[0], c[0])

        meas_sched = Play(Gaussian(1200, 0.2, 4), MeasureChannel(0))
        meas_sched |= Acquire(1200, AcquireChannel(0), MemorySlot(0))
        qc.add_calibration("measure", [0], meas_sched)

        sched = schedule(qc, self.backend)
        expected = Schedule(self.inst_map.get("u2", [0], 0, 0), (2, meas_sched))
        self.assertEqual(sched.instructions, expected.instructions)

    def test_subset_calibrated_measurements(self):
        """Test that measurement calibrations can be added and used for some qubits, even
        if the other qubits do not also have calibrated measurements."""
        qc = QuantumCircuit(3, 3)
        qc.measure(0, 0)
        qc.measure(1, 1)
        qc.measure(2, 2)
        meas_scheds = []
        for qubit in [0, 2]:
            meas = Play(Gaussian(1200, 0.2, 4), MeasureChannel(qubit)) + Acquire(
                1200, AcquireChannel(qubit), MemorySlot(qubit)
            )
            meas_scheds.append(meas)
            qc.add_calibration("measure", [qubit], meas)

        meas = macros.measure([1], FakeOpenPulse3Q())
        meas = meas.exclude(channels=[AcquireChannel(0), AcquireChannel(2)])
        sched = schedule(qc, FakeOpenPulse3Q())
        expected = Schedule(meas_scheds[0], meas_scheds[1], meas)
        self.assertEqual(sched.instructions, expected.instructions)

    def test_clbits_of_calibrated_measurements(self):
        """Test that calibrated measurements are only used when the classical bits also match."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.measure(q[0], c[1])

        meas_sched = Play(Gaussian(1200, 0.2, 4), MeasureChannel(0))
        meas_sched |= Acquire(1200, AcquireChannel(0), MemorySlot(0))
        qc.add_calibration("measure", [0], meas_sched)

        sched = schedule(qc, self.backend)
        # Doesn't use the calibrated schedule because the classical memory slots do not match
        expected = Schedule(macros.measure([0], self.backend, qubit_mem_slots={0: 1}))
        self.assertEqual(sched.instructions, expected.instructions)

    def test_metadata_is_preserved_alap(self):
        """Test that circuit metadata is preserved in output schedule with alap."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(U2Gate(0, 0), [q[1]])
        qc.metadata = {"experiment_type": "gst", "execution_number": "1234"}
        sched = schedule(qc, self.backend, method="alap")
        self.assertEqual({"experiment_type": "gst", "execution_number": "1234"}, sched.metadata)

    def test_metadata_is_preserved_asap(self):
        """Test that circuit metadata is preserved in output schedule with asap."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(U2Gate(0, 0), [q[1]])
        qc.metadata = {"experiment_type": "gst", "execution_number": "1234"}
        sched = schedule(qc, self.backend, method="asap")
        self.assertEqual({"experiment_type": "gst", "execution_number": "1234"}, sched.metadata)

    def test_scheduler_with_params_bound(self):
        """Test scheduler with parameters defined and bound"""
        x = Parameter("x")
        qc = QuantumCircuit(2)
        qc.append(Gate("pulse_gate", 1, [x]), [0])
        expected_schedule = Schedule()
        qc.add_calibration(gate="pulse_gate", qubits=[0], schedule=expected_schedule, params=[x])
        qc = qc.assign_parameters({x: 1})
        sched = schedule(qc, self.backend)
        self.assertEqual(sched, expected_schedule)

    def test_scheduler_with_params_not_bound(self):
        """Test scheduler with parameters defined but not bound"""
        x = Parameter("amp")
        qc = QuantumCircuit(2)
        qc.append(Gate("pulse_gate", 1, [x]), [0])
        with build() as expected_schedule:
            play(Gaussian(duration=160, amp=x, sigma=40), DriveChannel(0))
        qc.add_calibration(gate="pulse_gate", qubits=[0], schedule=expected_schedule, params=[x])
        sched = schedule(qc, self.backend)
        self.assertEqual(sched, transforms.target_qobj_transform(expected_schedule))

    def test_schedule_block_in_instmap(self):
        """Test schedule block in instmap can be scheduled."""
        duration = Parameter("duration")

        with build() as pulse_prog:
            play(Gaussian(duration, 0.1, 10), DriveChannel(0))

        instmap = InstructionScheduleMap()
        instmap.add("block_gate", (0,), pulse_prog, ["duration"])

        qc = QuantumCircuit(1)
        qc.append(Gate("block_gate", 1, [duration]), [0])
        qc.assign_parameters({duration: 100}, inplace=True)

        sched = schedule(qc, self.backend, inst_map=instmap)

        ref_sched = Schedule()
        ref_sched += Play(Gaussian(100, 0.1, 10), DriveChannel(0))

        self.assertEqual(sched, ref_sched)


class TestBasicScheduleV2(QiskitTestCase):
    """Scheduling tests."""

    def setUp(self):
        super().setUp()
        self.backend = FakePerth()
        self.inst_map = self.backend.instruction_schedule_map

    def test_alap_pass(self):
        """Test ALAP scheduling."""

        #       ┌────┐          ░      ┌─┐
        # q0_0: ┤ √X ├──────────░───■──┤M├───
        #       ├────┤ ░ ┌────┐ ░ ┌─┴─┐└╥┘┌─┐
        # q0_1: ┤ √X ├─░─┤ √X ├─░─┤ X ├─╫─┤M├
        #       └────┘ ░ └────┘ ░ └───┘ ║ └╥┘
        # c0: 2/════════════════════════╩══╩═
        #                               0  1

        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)

        qc.sx(q[0])
        qc.sx(q[1])
        qc.barrier(q[1])

        qc.sx(q[1])
        qc.barrier(q[0], q[1])

        qc.cx(q[0], q[1])
        qc.measure(q, c)

        sched = schedule(circuits=qc, backend=self.backend, method="alap")
        # Since, the method of scheduling chosen here is 'as_late_as_possible'
        # so all the π/2 pulse here should be right shifted.
        #
        # Calculations:
        #   Duration of the π/2 pulse for FakePerth backend is 160dt
        #   first π/2 pulse on q0 should start at 160dt because of 'as_late_as_possible'.
        #   first π/2 pulse on q1 should start 0dt.
        #   second π/2 pulse on q1 should start with a delay of 160dt.
        #   cx pulse( pulse on drive channel, control channel) should start with a delay
        #   of 160dt+160dt.
        #   measure pulse should start with a delay of 160dt+160dt+1760dt(1760dt for cx gate).
        expected = Schedule(
            (0, self.inst_map.get("sx", [1])),
            (0 + 160, self.inst_map.get("sx", [0])),  # Right shifted because of alap.
            (0 + 160, self.inst_map.get("sx", [1])),
            (0 + 160 + 160, self.inst_map.get("cx", [0, 1])),
            (0 + 160 + 160 + 1760, Acquire(1472, AcquireChannel(0), MemorySlot(0))),
            (0 + 160 + 160 + 1760, Acquire(1472, AcquireChannel(1), MemorySlot(1))),
            (
                0 + 160 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.24000000000000002,
                        angle=-0.24730169436555283,
                        name="M_m0",
                    ),
                    MeasureChannel(0),
                    name="M_m0",
                ),
            ),
            (
                0 + 160 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.32,
                        angle=-1.9900962136758156,
                        name="M_m1",
                    ),
                    MeasureChannel(1),
                    name="M_m1",
                ),
            ),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_single_circuit_list_schedule(self):
        """Test that passing a single circuit list to schedule() returns a list."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        sched = schedule([qc], self.backend, method="alap")
        expected = Schedule()
        self.assertIsInstance(sched, list)
        self.assertEqual(sched[0].instructions, expected.instructions)

    def test_alap_with_barriers(self):
        """Test that ALAP respects barriers on new qubits."""

        #       ┌────┐ ░
        # q0_0: ┤ √X ├─░───────
        #       └────┘ ░ ┌────┐
        # q0_1: ───────░─┤ √X ├
        #              ░ └────┘
        # c0: 2/═══════════════
        #

        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.append(SXGate(), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(SXGate(), [q[1]])
        sched = schedule(qc, self.backend, method="alap")
        # If there wasn't a barrier the π/2 pulse on q1 would have started from 0dt, but since,
        # there is a barrier so the π/2 pulse on q1 should start with a delay of 160dt.
        expected = Schedule((0, self.inst_map.get("sx", [0])), (160, self.inst_map.get("sx", [1])))
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual, expected)

    def test_empty_circuit_schedule(self):
        """Test empty circuit being scheduled."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule()
        self.assertEqual(sched.instructions, expected.instructions)

    def test_alap_aligns_end(self):
        """Test that ALAP always acts as though there is a final global barrier."""
        #       ┌────┐
        # q1_0: ┤ √X ├
        #       ├────┤
        # q1_1: ┤ √X ├
        #       └────┘
        # c1: 2/══════

        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.sx(q[0])
        qc.sx(q[1])
        sched = schedule(qc, self.backend, method="alap")
        expected_sched = Schedule(
            (0, self.inst_map.get("sx", [1])), (0, self.inst_map.get("sx", [0]))
        )
        for actual, expected in zip(sched.instructions, expected_sched.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])
        self.assertEqual(
            sched.ch_duration(DriveChannel(0)), expected_sched.ch_duration(DriveChannel(1))
        )

    def test_asap_pass(self):
        """Test ASAP scheduling."""

        #       ┌────┐          ░      ┌─┐
        # q0_0: ┤ √X ├──────────░───■──┤M├───
        #       ├────┤ ░ ┌────┐ ░ ┌─┴─┐└╥┘┌─┐
        # q0_1: ┤ √X ├─░─┤ √X ├─░─┤ X ├─╫─┤M├
        #       └────┘ ░ └────┘ ░ └───┘ ║ └╥┘
        # c0: 2/════════════════════════╩══╩═
        #                               0  1

        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)

        qc.sx(q[0])
        qc.sx(q[1])
        qc.barrier(q[1])

        qc.sx(q[1])
        qc.barrier(q[0], q[1])

        qc.cx(q[0], q[1])
        qc.measure(q, c)

        sched = schedule(circuits=qc, backend=self.backend, method="asap")
        # Since, the method of scheduling chosen here is 'as_soon_as_possible'
        # so all the π/2 pulse here should be left shifted.
        #
        # Calculations:
        #   Duration of the π/2 pulse for FakePerth backend is 160dt
        #   first π/2 pulse on q0 should start at 0dt because of 'as_soon_as_possible'.
        #   first π/2 pulse on q1 should start 0dt.
        #   second π/2 pulse on q1 should start with a delay of 160dt.
        #   cx pulse( pulse on drive channel, control channel) should start with a delay
        #   of 160dt+160dt.
        #   measure pulse should start with a delay of 160dt+160dt+1760dt(1760dt for cx gate).
        expected = Schedule(
            (0, self.inst_map.get("sx", [1])),
            (0, self.inst_map.get("sx", [0])),  # Left shifted because of asap.
            (0 + 160, self.inst_map.get("sx", [1])),
            (0 + 160 + 160, self.inst_map.get("cx", [0, 1])),
            (0 + 160 + 160 + 1760, Acquire(1472, AcquireChannel(0), MemorySlot(0))),
            (0 + 160 + 160 + 1760, Acquire(1472, AcquireChannel(1), MemorySlot(1))),
            (
                0 + 160 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.24000000000000002,
                        angle=-0.24730169436555283,
                        name="M_m0",
                    ),
                    MeasureChannel(0),
                    name="M_m0",
                ),
            ),
            (
                0 + 160 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.32,
                        angle=-1.9900962136758156,
                        name="M_m1",
                    ),
                    MeasureChannel(1),
                    name="M_m1",
                ),
            ),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_alap_resource_respecting(self):
        """Test that the ALAP pass properly respects busy resources when backwards scheduling.
        For instance, a CX on 0 and 1 followed by an X on only 1 must respect both qubits'
        timeline."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.sx(q[1])
        sched = schedule(qc, self.backend, method="as_late_as_possible")
        insts = sched.instructions

        # This is ShiftPhase for the cx operation.
        self.assertEqual(insts[0][0], 0)

        # It takes 9 pulse operations on DriveChannel and ControlChannel to do a
        # cx operation on this backend.
        # 1760dt is duration of cx operation on this backend.
        self.assertEqual(insts[9][0], 1760)

        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.sx(q[1])
        qc.measure(q, c)
        sched = schedule(qc, self.backend, method="as_late_as_possible")

        # 1760dt for cx operation + 160dt for sx operation + 1472dt for measure on both qubits.
        # Total: 3392dt.
        # So, the delay in MeasureChannel0 and 1 starts from 3392dt.
        self.assertEqual(sched.instructions[-1][0], 3392)

    def test_inst_map_schedules_unaltered(self):
        """Test that forward scheduling doesn't change relative timing with a command."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        sched1 = schedule(qc, self.backend, method="as_soon_as_possible")
        sched2 = schedule(qc, self.backend, method="as_late_as_possible")
        for asap, alap in zip(sched1.instructions, sched2.instructions):
            self.assertEqual(asap[0], alap[0])
            self.assertEqual(asap[1], alap[1])
        insts = sched1.instructions
        self.assertEqual(insts[0][0], 0)  # ShiftPhase at DriveChannel(0) no dt required.
        self.assertEqual(insts[1][0], 0)  # ShiftPhase at ControlChannel(1) no dt required.
        self.assertEqual(insts[2][0], 0)  # Pulse Ym_d0 of duration 160dt.
        self.assertEqual(insts[3][0], 0)  # Pulse X90p_d1 of duration 160dt.
        self.assertEqual(insts[4][0], 160)  # Pusle CR90p_d1_u0 of duration 720dt.
        self.assertEqual(insts[5][0], 160)  # Pulse CR90p_u0 of duration 720dt.
        self.assertEqual(insts[6][0], 880)  # Pulse Xp_d0 of duratoin 160dt.
        self.assertEqual(insts[7][0], 1040)  # Pulse CR90m_d1_u0 of duration 720dt.
        self.assertEqual(insts[8][0], 1040)  # Pulse CR90m_u0 of duration 720dt.

    def test_measure_combined(self):
        """
        Test to check for measure on the same qubit which generated another measure schedule.

        The measures on different qubits are combined, but measures on the same qubit
        adds another measure to the schedule.
        """
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.sx(q[0])
        qc.cx(q[0], q[1])
        qc.measure(q[0], c[0])
        qc.measure(q[1], c[1])
        qc.measure(q[1], c[1])
        sched = schedule(qc, self.backend, method="as_soon_as_possible")

        expected_sched = Schedule(
            # This is the Schedule to implement sx gate.
            (0, self.inst_map.get("sx", [0])),
            # This is the schedule to implement cx gate
            (0 + 160, self.inst_map.get("cx", [0, 1])),
            # Getting ready the to take classical data in MemorySlot(0) for qubit:0
            (0 + 160 + 1760, Acquire(1472, AcquireChannel(0), MemorySlot(0))),
            # Since, the method of Scheduling is 'as_soon_as_possilbe' so again for
            # qubit:1 getting ready the to take classical data in MemorySlot(1)
            (0 + 160 + 1760, Acquire(1472, AcquireChannel(1), MemorySlot(1))),
            # Playing pulse on MeasureChannel(0) to read qubit:0 to MemoryChannel(0)
            (
                0 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.24000000000000002,
                        angle=-0.24730169436555283,
                        name="M_m0",
                    ),
                    MeasureChannel(0),
                    name="M_m0",
                ),
            ),
            # Playing pulse on MeasureChannel(1) to read qubit:1 to MemoryChannel(1)
            (
                0 + 160 + 1760,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.32,
                        angle=-1.9900962136758156,
                        name="M_m1",
                    ),
                    MeasureChannel(1),
                    name="M_m1",
                ),
            ),
            # Time for MeasureChannel(0) to relax
            (0 + 160 + 1760 + 1472, Delay(1568, MeasureChannel(0))),
            # Time for MeasureChannel(1) to relax
            (0 + 160 + 1760 + 1472, Delay(1568, MeasureChannel(1))),
            # The below schedules would be for the additional measure(1, 1) instruction added.
            (0 + 160 + 1760 + 1472 + 1568, Acquire(1472, AcquireChannel(1), MemorySlot(1))),
            (
                0 + 160 + 1760 + 1472 + 1568,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.32,
                        angle=-1.9900962136758156,
                        name="M_m1",
                    ),
                    MeasureChannel(1),
                    name="M_m1",
                ),
            ),
            (0 + 160 + 1760 + 1472 + 1568 + 1472, Delay(1568, MeasureChannel(1))),
        )
        self.assertEqual(sched.instructions, expected_sched.instructions)

    def test_3q_schedule(self):
        """Test a schedule that was recommended by David McKay :D"""

        #             ┌────┐
        # q0_0: ──■───┤ √X ├───────────
        #       ┌─┴─┐ ├───┬┘
        # q0_1: ┤ X ├─┤ X ├───■────────
        #       ├───┴┐└───┘ ┌─┴─┐┌────┐
        # q0_2: ┤ √X ├──────┤ X ├┤ √X ├
        #       └────┘      └───┘└────┘
        # c0: 3/═══════════════════════

        q = QuantumRegister(3)
        c = ClassicalRegister(3)
        qc = QuantumCircuit(q, c)
        qc.cx(q[0], q[1])
        qc.sx(q[0])
        qc.x(q[1])
        qc.sx(q[2])
        qc.cx(q[1], q[2])
        qc.sx(q[2])

        sched = schedule(qc, self.backend, method="asap")
        expected = Schedule(
            (0, self.inst_map.get("cx", [0, 1])),
            (0, self.inst_map.get("sx", [2])),
            (0 + 1760, self.inst_map.get("sx", [0])),
            (0 + 1760, self.inst_map.get("x", [1])),
            (0 + 1760 + 160, self.inst_map.get("cx", [1, 2])),
            (0 + 1760 + 1760, self.inst_map.get("sx", [2])),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_schedule_multi(self):
        """Test scheduling multiple circuits at once."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc0 = QuantumCircuit(q, c)
        qc0.cx(q[0], q[1])
        qc1 = QuantumCircuit(q, c)
        qc1.cx(q[0], q[1])
        schedules = schedule([qc0, qc1], self.backend)
        expected_insts = schedule(qc0, self.backend).instructions
        for actual, expected in zip(schedules[0].instructions, expected_insts):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_circuit_name_kept(self):
        """Test that the new schedule gets its name from the circuit."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c, name="CIRCNAME")
        qc.cx(q[0], q[1])
        sched = schedule(qc, self.backend, method="asap")
        self.assertEqual(sched.name, qc.name)
        sched = schedule(qc, self.backend, method="alap")
        self.assertEqual(sched.name, qc.name)

    def test_can_add_gates_into_free_space(self):
        """The scheduler does some time bookkeeping to know when qubits are free to be
        scheduled. Make sure this works for qubits that are used in the future. This was
        a bug, uncovered by this example:

           q0 =  - - - - |X|
           q1 = |X| |u2| |X|

        In ALAP scheduling, the next operation on qubit 0 would be added at t=0 rather
        than immediately before the X gate.
        """
        qr = QuantumRegister(2)
        qc = QuantumCircuit(qr)
        for i in range(2):
            qc.sx(qr[i])
            qc.x(qr[i])
            qc.sx(qr[i])
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule(
            (0, self.inst_map.get("sx", [0])),
            (0, self.inst_map.get("sx", [1])),
            (0 + 160, self.inst_map.get("x", [0])),
            (0 + 160, self.inst_map.get("x", [1])),
            (0 + 160 + 160, self.inst_map.get("sx", [0])),
            (0 + 160 + 160, self.inst_map.get("sx", [1])),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_barriers_in_middle(self):
        """As a follow on to `test_can_add_gates_into_free_space`, similar issues
        arose for barriers, specifically.
        """
        qr = QuantumRegister(2)
        qc = QuantumCircuit(qr)
        for i in range(2):
            qc.sx(qr[i])
            qc.barrier(qr[i])
            qc.x(qr[i])
            qc.barrier(qr[i])
            qc.sx(qr[i])
        sched = schedule(qc, self.backend, method="alap")
        expected = Schedule(
            (0, self.inst_map.get("sx", [0])),
            (0, self.inst_map.get("sx", [1])),
            (0 + 160, self.inst_map.get("x", [0])),
            (0 + 160, self.inst_map.get("x", [1])),
            (0 + 160 + 160, self.inst_map.get("sx", [0])),
            (0 + 160 + 160, self.inst_map.get("sx", [1])),
        )
        for actual, expected in zip(sched.instructions, expected.instructions):
            self.assertEqual(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])

    def test_parametric_input(self):
        """Test that scheduling works with parametric pulses as input."""
        qr = QuantumRegister(1)
        qc = QuantumCircuit(qr)
        qc.append(Gate("gauss", 1, []), qargs=[qr[0]])
        custom_gauss = Schedule(
            Play(Gaussian(duration=25, sigma=4, amp=0.5, angle=pi / 2), DriveChannel(0))
        )
        self.inst_map.add("gauss", [0], custom_gauss)
        sched = schedule(qc, self.backend, inst_map=self.inst_map)
        self.assertEqual(sched.instructions[0], custom_gauss.instructions[0])

    def test_pulse_gates(self):
        """Test scheduling calibrated pulse gates."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.append(U2Gate(0, 0), [q[0]])
        qc.barrier(q[0], q[1])
        qc.append(U2Gate(0, 0), [q[1]])
        qc.add_calibration("u2", [0], Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(0))), [0, 0])
        qc.add_calibration("u2", [1], Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(1))), [0, 0])

        sched = schedule(qc, self.backend)
        expected = Schedule(
            Play(Gaussian(28, 0.2, 4), DriveChannel(0)),
            (28, Schedule(Play(Gaussian(28, 0.2, 4), DriveChannel(1)))),
        )
        self.assertEqual(sched.instructions, expected.instructions)

    def test_calibrated_measurements(self):
        """Test scheduling calibrated measurements."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.sx(0)
        qc.measure(q[0], c[0])

        meas_sched = Play(
            GaussianSquare(
                duration=1472,
                sigma=64,
                width=1216,
                amp=0.2400000000002,
                angle=-0.247301694,
                name="my_custom_calibration",
            ),
            MeasureChannel(0),
        )
        meas_sched |= Acquire(1472, AcquireChannel(0), MemorySlot(0))
        qc.add_calibration("measure", [0], meas_sched)

        sched = schedule(qc, self.backend)
        expected = Schedule(self.inst_map.get("sx", [0]), (160, meas_sched))
        self.assertEqual(sched.instructions, expected.instructions)

    def test_subset_calibrated_measurements(self):
        """Test that measurement calibrations can be added and used for some qubits, even
        if the other qubits do not also have calibrated measurements."""
        qc = QuantumCircuit(3, 3)
        qc.measure(0, 0)
        qc.measure(1, 1)
        qc.measure(2, 2)
        meas_scheds = []
        for qubit in [0, 2]:
            meas = Play(Gaussian(1200, 0.2, 4), MeasureChannel(qubit)) + Acquire(
                1200, AcquireChannel(qubit), MemorySlot(qubit)
            )
            meas_scheds.append(meas)
            qc.add_calibration("measure", [qubit], meas)

        meas = macros.measure(qubits=[1], backend=self.backend, qubit_mem_slots={0: 0, 1: 1})
        meas = meas.exclude(channels=[AcquireChannel(0), AcquireChannel(2)])
        sched = schedule(qc, self.backend)
        expected = Schedule(meas_scheds[0], meas_scheds[1], meas)
        self.assertEqual(sched.instructions, expected.instructions)

    def test_clbits_of_calibrated_measurements(self):
        """Test that calibrated measurements are only used when the classical bits also match."""
        q = QuantumRegister(2)
        c = ClassicalRegister(2)
        qc = QuantumCircuit(q, c)
        qc.measure(q[0], c[1])

        meas_sched = Play(Gaussian(1200, 0.2, 4), MeasureChannel(0))
        meas_sched |= Acquire(1200, AcquireChannel(0), MemorySlot(0))
        qc.add_calibration("measure", [0], meas_sched)

        sched = schedule(qc, self.backend)
        # Doesn't use the calibrated schedule because the classical memory slots do not match
        expected = Schedule(macros.measure([0], self.backend, qubit_mem_slots={0: 1}))
        self.assertEqual(sched.instructions, expected.instructions)

    def test_metadata_is_preserved_alap(self):
        """Test that circuit metadata is preserved in output schedule with alap."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.sx(q[0])
        qc.barrier(q[0], q[1])
        qc.sx(q[1])
        qc.metadata = {"experiment_type": "gst", "execution_number": "1234"}
        sched = schedule(qc, self.backend, method="alap")
        self.assertEqual({"experiment_type": "gst", "execution_number": "1234"}, sched.metadata)

    def test_metadata_is_preserved_asap(self):
        """Test that circuit metadata is preserved in output schedule with asap."""
        q = QuantumRegister(2)
        qc = QuantumCircuit(q)
        qc.sx(q[0])
        qc.barrier(q[0], q[1])
        qc.sx(q[1])
        qc.metadata = {"experiment_type": "gst", "execution_number": "1234"}
        sched = schedule(qc, self.backend, method="asap")
        self.assertEqual({"experiment_type": "gst", "execution_number": "1234"}, sched.metadata)

    def test_scheduler_with_params_bound(self):
        """Test scheduler with parameters defined and bound"""
        x = Parameter("x")
        qc = QuantumCircuit(2)
        qc.append(Gate("pulse_gate", 1, [x]), [0])
        expected_schedule = Schedule()
        qc.add_calibration(gate="pulse_gate", qubits=[0], schedule=expected_schedule, params=[x])
        qc = qc.assign_parameters({x: 1})
        sched = schedule(qc, self.backend)
        self.assertEqual(sched, expected_schedule)

    def test_scheduler_with_params_not_bound(self):
        """Test scheduler with parameters defined but not bound"""
        x = Parameter("amp")
        qc = QuantumCircuit(2)
        qc.append(Gate("pulse_gate", 1, [x]), [0])
        with build() as expected_schedule:
            play(Gaussian(duration=160, amp=x, sigma=40), DriveChannel(0))
        qc.add_calibration(gate="pulse_gate", qubits=[0], schedule=expected_schedule, params=[x])
        sched = schedule(qc, self.backend)
        self.assertEqual(sched, transforms.target_qobj_transform(expected_schedule))

    def test_schedule_block_in_instmap(self):
        """Test schedule block in instmap can be scheduled."""
        duration = Parameter("duration")

        with build() as pulse_prog:
            play(Gaussian(duration, 0.1, 10), DriveChannel(0))

        instmap = InstructionScheduleMap()
        instmap.add("block_gate", (0,), pulse_prog, ["duration"])

        qc = QuantumCircuit(1)
        qc.append(Gate("block_gate", 1, [duration]), [0])
        qc.assign_parameters({duration: 100}, inplace=True)

        sched = schedule(qc, self.backend, inst_map=instmap)

        ref_sched = Schedule()
        ref_sched += Play(Gaussian(100, 0.1, 10), DriveChannel(0))

        self.assertEqual(sched, ref_sched)

    def test_inst_sched_map_get_measure_0(self):
        """Test that Schedule returned by backend.instruction_schedule_map.get('measure', [0])
        is actually Schedule for just qubit_0"""
        sched_from_backend = self.backend.instruction_schedule_map.get("measure", [0])
        expected_sched = Schedule(
            (0, Acquire(1472, AcquireChannel(0), MemorySlot(0))),
            (0, Acquire(1472, AcquireChannel(1), MemorySlot(1))),
            (0, Acquire(1472, AcquireChannel(2), MemorySlot(2))),
            (0, Acquire(1472, AcquireChannel(3), MemorySlot(3))),
            (0, Acquire(1472, AcquireChannel(4), MemorySlot(4))),
            (0, Acquire(1472, AcquireChannel(5), MemorySlot(5))),
            (0, Acquire(1472, AcquireChannel(6), MemorySlot(6))),
            (
                0,
                Play(
                    GaussianSquare(
                        duration=1472,
                        sigma=64,
                        width=1216,
                        amp=0.24000000000000002,
                        angle=-0.24730169436555283,
                        name="M_m0",
                    ),
                    MeasureChannel(0),
                    name="M_m0",
                ),
            ),
            (1472, Delay(1568, MeasureChannel(0))),
            name="measure",
        )
        self.assertEqual(sched_from_backend, expected_sched)
