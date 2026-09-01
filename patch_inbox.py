from pathlib import Path
p=Path(r"C:\devprojects\marginmise\auto_upload.py")
s=p.read_text(encoding="utf-8")
old='''    (folder / ".restaurant_workspace.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    readme = f"""{restaurant_name} AUTOMATIC UPLOAD FOLDER
'''
new='''    marker_path = folder / ".restaurant_workspace.json"
    marker_text = json.dumps(marker, indent=2)
    try:
        if marker_path.read_text(encoding="utf-8") != marker_text:
            marker_path.write_text(marker_text, encoding="utf-8")
    except (FileNotFoundError, OSError):
        marker_path.write_text(marker_text, encoding="utf-8")
    readme = f"""{restaurant_name} AUTOMATIC UPLOAD FOLDER
'''
assert old in s
s=s.replace(old,new,1)
old2='''    (folder / "README_DROP_FILES_HERE.txt").write_text(readme, encoding="utf-8")
'''
new2='''    readme_path = folder / "README_DROP_FILES_HERE.txt"
    try:
        if readme_path.read_text(encoding="utf-8") != readme:
            readme_path.write_text(readme, encoding="utf-8")
    except (FileNotFoundError, OSError):
        readme_path.write_text(readme, encoding="utf-8")
'''
assert old2 in s
p.write_text(s.replace(old2,new2,1),encoding="utf-8")
print("patched inbox initialization")
