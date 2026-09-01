@echo off
cd /d C:\devprojects\marginmise
.buildvenv\Scripts\python.exe -m PyInstaller marginmise_dir.spec --noconfirm --distpath dist_fixed3 --workpath build_fixed3
