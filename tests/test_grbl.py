import time

from laserburn.grbl import Callbacks, GrblController, clean_line, parse_status
from laserburn.grbl_sim import GrblSimulator


class Recorder:
    def __init__(self):
        self.lines, self.sent, self.statuses, self.progress, self.done = [], [], [], [], []

    def callbacks(self):
        return Callbacks(on_line=self.lines.append, on_sent=self.sent.append,
                         on_status=self.statuses.append,
                         on_progress=lambda a, b: self.progress.append((a, b)),
                         on_job_done=lambda ok, msg: self.done.append((ok, msg)))


def run_until(ctrl, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctrl.step()
        if cond():
            return True
    return False


class PingPongSim(GrblSimulator):
    """Проверяет, что у станка никогда нет больше одной неподтверждённой строки."""
    max_in_flight = 0

    def write(self, data):
        r = super().write(data)
        in_flight = len(self._pending)
        PingPongSim.max_in_flight = max(PingPongSim.max_in_flight, in_flight)
        return r


def test_parse_status_with_wco():
    st, wco = parse_status("<Run|MPos:12.000,5.500,0.000|FS:1000,500|WCO:2.000,0.500,0.000>", (0, 0, 0))
    assert st.state == "Run"
    assert st.mpos == (12, 5.5, 0)
    assert st.wpos == (10, 5, 0)
    assert (st.feed, st.spindle) == (1000, 500)
    assert wco == (2, 0.5, 0)
    # WCO не присылается в каждом отчёте — используется последний известный
    st2, _ = parse_status("<Idle|MPos:3,3,0|FS:0,0>", wco)
    assert st2.wpos == (1, 2.5, 0)
    st3, _ = parse_status("<Hold:0|WPos:1,1,0|FS:0,0>", (1, 1, 0))
    assert st3.state == "Hold" and st3.mpos == (2, 2, 0)


def test_clean_line():
    assert clean_line("G1 X1 ; comment") == "G1 X1"
    assert clean_line("(header) G0 X0") == "G0 X0"
    assert clean_line("; only") == ""


def test_job_streams_ping_pong():
    sim = PingPongSim()
    rec = Recorder()
    ctrl = GrblController(sim, rec.callbacks())
    job = ["; заголовок", "G21", "G90", "M4 S0"] + [f"G1 X{i} Y{i} S100 F1000" for i in range(1, 51)] + ["M5"]
    ctrl.start_job(job)
    assert run_until(ctrl, lambda: rec.done)
    assert rec.done == [(True, rec.done[0][1])] and rec.done[0][0]
    assert sim.received == [x for x in job if not x.startswith(";")]
    assert PingPongSim.max_in_flight <= 1
    assert rec.progress[-1] == (54, 54)
    assert sim.pos[:2] == [50.0, 50.0]


def test_manual_command_and_status():
    sim = GrblSimulator()
    rec = Recorder()
    ctrl = GrblController(sim, rec.callbacks())
    ctrl.send("$J=G91 G21 X10 Y-5 F3000")
    assert run_until(ctrl, lambda: "ok" in rec.lines and rec.statuses and rec.statuses[-1].mpos[0] == 10)
    assert rec.statuses[-1].mpos[:2] == (10, -5)
    assert sim.absolute  # jog не меняет G90


def test_pause_resume():
    sim = GrblSimulator(line_delay=0.01)
    rec = Recorder()
    ctrl = GrblController(sim, rec.callbacks())
    ctrl.start_job([f"G1 X{i}" for i in range(30)])
    run_until(ctrl, lambda: len(sim.received) >= 5)
    ctrl.pause()
    run_until(ctrl, lambda: False, timeout=0.2)
    n = len(sim.received)
    run_until(ctrl, lambda: False, timeout=0.2)
    assert len(sim.received) == n and sim.hold
    ctrl.resume()
    assert run_until(ctrl, lambda: rec.done)
    assert rec.done[0][0] and len(sim.received) == 30


def test_stop_resets_machine():
    sim = GrblSimulator(line_delay=0.01)
    rec = Recorder()
    ctrl = GrblController(sim, rec.callbacks())
    ctrl.start_job([f"G1 X{i} S500 F100" for i in range(100)])
    run_until(ctrl, lambda: len(sim.received) >= 3)
    ctrl.stop()
    assert run_until(ctrl, lambda: rec.lines.count("Grbl 1.1h ['$' for help]") >= 2)
    assert rec.done and rec.done[0][0] is False
    assert not ctrl.job_running and not sim.laser_on and len(sim.received) < 100
    # После остановки ручные команды снова работают
    ctrl.send("G0 X0")
    assert run_until(ctrl, lambda: sim.received[-1] == "G0 X0")


def test_error_aborts_job():
    sim = GrblSimulator()
    rec = Recorder()
    ctrl = GrblController(sim, rec.callbacks())
    ctrl.start_job(["G1 X1", "ERR", "G1 X2"])
    assert run_until(ctrl, lambda: rec.done)
    ok, msg = rec.done[0]
    assert not ok and "ERR" in msg
    run_until(ctrl, lambda: False, timeout=0.6)
    assert "G1 X2" not in sim.received
