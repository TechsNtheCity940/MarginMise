@echo off
cd /d C:\devprojects\marginmise
.buildvenv\Scripts\python.exe -m PyInstaller marginmise_dir.spec --clean --noconfirm --distpath dist_fixed2 --workpath build_fixed2
