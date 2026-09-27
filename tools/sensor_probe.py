"""Debug helper: print what the selected sensor backend actually reports."""
import sys, time
sys.path.insert(0, ".")  # our tree first: vendor has its own main.py
sys.path.append("vendor/turing-smart-screen-python")
from app import config as cfgmod
from app.sensors import make_hub
from app.power import estimate

cfg = cfgmod.load()
backend = sys.argv[1] if len(sys.argv) > 1 else "lhm"
hub = make_hub(cfg, force=backend)
hub.tick(); time.sleep(0.5)
snap = hub.tick()
est = estimate(snap, cfg)
print(f"backend={backend}")
print(f"cpu: load={snap.cpu.load_pct} temp={snap.cpu.temp_c} "
      f"clk_max={snap.cpu.clock_max_mhz} clk_avg={snap.cpu.clock_avg_mhz} power={snap.cpu.power_w}")
print(f"gpu: load={snap.gpu.load_pct} temp={snap.gpu.temp_c} mhz={snap.gpu.core_mhz} "
      f"power={snap.gpu.power_w} vram={snap.gpu.vram_used_mb}/{snap.gpu.vram_total_mb}")
print(f"ram: {snap.ram_used_mb}/{snap.ram_total_mb}  disk r/w={snap.disk_read_bps}/{snap.disk_write_bps}")
print(f"net d/u={snap.net_down_bps}/{snap.net_up_bps} bit/s (panel: Mbps)   "
      f"disk r/w in bytes/s")
# The estimate says what it knows: total is None when there is no honest
# number, and sources/status say measured vs modelled vs unknown per part.
tot = "none" if est.total_w is None else f"{est.total_w:.0f}W"
print(f"power total={tot} status={est.status} sources={est.sources} "
      f"age={est.age_s if est.age_s is None else round(est.age_s, 1)}s "
      f"parts={est.parts}")
