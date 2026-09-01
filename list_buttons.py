import ast
from pathlib import Path
files=[Path(r"C:\devprojects\marginmise\manager_first_gui.py"),Path(r"C:\devprojects\marginmise\restaurant_cost_gui.py")]
for p in files:
    tree=ast.parse(p.read_text(encoding="utf-8"))
    print("FILE",p.name)
    n=0
    for x in ast.walk(tree):
        if isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute) and x.func.attr in ("Button","Checkbutton","Menubutton"):
            text=cmd=""
            for kw in x.keywords:
                if kw.arg=="text": text=ast.unparse(kw.value)
                if kw.arg=="command": cmd=ast.unparse(kw.value)
            print(f"{n+1:03} {x.func.attr}: {text} | {cmd}")
            n+=1
    print("COUNT",n)
