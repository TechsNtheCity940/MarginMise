from pathlib import Path
p=Path(r"C:\devprojects\marginmise\auto_upload.py")
s=p.read_text(encoding="utf-8")
old='''    readme_path = folder / "README_DROP_FILES_HERE.txt"
    try:
        if readme_path.read_text(encoding="utf-8") != readme:
            readme_path.write_text(readme, encoding="utf-8")
    except (FileNotFoundError, OSError):
        readme_path.write_text(readme, encoding="utf-8")
'''
new='''    # Do not rewrite a Desktop file on every application launch. Security tools
    # commonly flag repeated writes/deletes in protected Desktop locations.
    # The README is optional documentation and is created only when explicitly
    # requested by the user/environment.
    readme_path = folder / "README_DROP_FILES_HERE.txt"
    if os.environ.get("MARGINMISE_CREATE_UPLOAD_README", "0") == "1" and not readme_path.exists():
        try:
            readme_path.write_text(readme, encoding="utf-8")
        except OSError:
            pass
'''
assert old in s
p.write_text(s.replace(old,new,1),encoding="utf-8")
print("patched README creation to opt-in")
