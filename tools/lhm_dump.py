"""Debug helper: dump every hardware + sensor LibreHardwareMonitor sees.
Run elevated and redirect to a file to see what ring0 exposes."""
import sys, time
sys.path.insert(0, ".")
sys.path.insert(0, "vendor/turing-smart-screen-python")
from app import config as cfgmod
from app.sensors.lhm import LhmBackend

cfg = cfgmod.load()
b = LhmBackend(cfg)

for hw in b._c.Hardware:
    print(f"\n== {hw.Name} [{hw.HardwareType}]")
    def show(h, indent="  "):
        for s in h.Sensors:
            v = f"{s.Value:.2f}" if s.Value is not None else "None"
            print(f"{indent}{s.SensorType,-14} {s.Name,-28} {v}")
        for sub in h.SubHardware:
            print(f"{indent}-- {sub.Name}")
            show(sub, indent + "    ")
    show(hw)
b._c.Close()
