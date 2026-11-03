@echo off
git init -b main
git config user.email "aimehas@icloud.com"
git config user.name "Hastwryn"
git add -A
git commit -m "Initial commit"
git remote add origin https://github.com/Hastwryn/VoxPop.git
git push -u origin main
pause