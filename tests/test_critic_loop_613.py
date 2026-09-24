import importlib.util
from pathlib import Path
R=Path(__file__).resolve().parents[1];s=importlib.util.spec_from_file_location("c",R/"tools/critic_loop.py");c=importlib.util.module_from_spec(s);s.loader.exec_module(c);r=c.run(skip=True,write=False);assert r["verdict"]=="pass" and len(r["rounds"])==3 and r["rounds"][1]["verdict"]=="skipped";print("PASS three-round product critic structure and opportunity review")
