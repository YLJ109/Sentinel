@echo off
setlocal
title 守望 Sentinel · 一键部署

rem ===========================================================================
rem  守望 Sentinel · 校园反霸凌智能检测系统 —— 一键部署（生产模式）
rem
rem  作用：生产配置预检 -^> 构建前端静态资源 -^> 拉起后端(8000) 与前端(4173)
rem  用法：直接双击本文件。
rem
rem  说明：前端以「构建产物 + 本地静态服务」运行，适合单机与校园内网部署；
rem        若需对外正式发布，建议改用 Nginx 托管 frontend\dist（见脚本末尾提示）。
rem ===========================================================================

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo.
echo  ==============================================================
echo    守望 Sentinel · 校园反霸凌智能检测系统
echo    一键部署（生产模式）
echo  ==============================================================
echo.

rem ---------- 1/4 环境自检 ----------
call :require "backend\.venv\Scripts\python.exe" "后端虚拟环境" "请先在 backend 目录执行 python -m venv .venv，再执行 .venv\Scripts\python -m pip install -r requirements.txt"
if errorlevel 1 goto :abort

call :require "frontend\node_modules" "前端依赖" "请先在 frontend 目录执行 npm install"
if errorlevel 1 goto :abort

where node >nul 2>nul
if errorlevel 1 (
    echo  [错误] 未检测到 Node.js，无法构建前端。
    echo         请安装 Node.js 18 或更高版本后重试。
    echo.
    goto :abort
)

if not exist "backend\.env" (
    copy /y "backend\.env.example" "backend\.env" >nul
    echo  [提示] 未找到 backend\.env，已从 .env.example 生成一份。
    echo         ** 生产部署前请务必填写真实配置 **，否则预检会全部报警。
    echo.
)

rem ---------- 2/4 生产配置预检 ----------
echo  [预检] 检查 backend\.env 中的生产关键项...
call :check_prod
echo.

rem ---------- 3/4 构建前端 ----------
echo  [1/3] 正在构建前端静态资源...
pushd "%ROOT%frontend"
call npm run build
if errorlevel 1 (
    popd
    echo.
    echo  [错误] 前端构建失败，请查看上方错误输出。
    goto :abort
)
popd
echo        构建完成，产物目录：frontend\dist
echo.

rem ---------- 4/4 启动服务 ----------
echo  [2/3] 正在启动后端服务（http://localhost:8000）...
start "守望 · 后端服务" /D "%ROOT%backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
call :wait_port 8000 150
if errorlevel 1 (
    echo  [警告] 后端 150 秒内未就绪。
    echo         若 CAB_ENV=production，配置校验不通过会直接退出且不监听端口，
    echo         请查看「守望 · 后端服务」窗口中的具体原因。
    echo.
) else (
    echo        后端已就绪。
)

echo  [3/3] 正在启动前端服务（http://localhost:4173）...
start "守望 · 前端服务" /D "%ROOT%frontend" cmd /k "npm run preview"
call :wait_port 4173 90
if errorlevel 1 (
    echo  [警告] 前端 90 秒内未就绪，请查看「守望 · 前端服务」窗口中的报错信息。
    echo.
) else (
    echo        前端已就绪。
)

start "" http://localhost:4173

echo.
echo  ==============================================================
echo    部署完成
echo  --------------------------------------------------------------
echo    访问地址  http://localhost:4173
echo    接口文档  http://localhost:8000/docs（CAB_ENV=production 时自动关闭）
echo    默认账号  admin / admin123（生产环境启动时会强制要求更换口令）
echo  --------------------------------------------------------------
echo    停止服务  关闭「守望 · 后端服务」与「守望 · 前端服务」两个窗口即可
echo  --------------------------------------------------------------
echo    正式对外发布：用 Nginx 托管 frontend\dist，并把 /api 反向代理到
echo    http://127.0.0.1:8000。WebSocket 路径 /api/ws/ 需带升级头：
echo        proxy_set_header Upgrade $http_upgrade;
echo        proxy_set_header Connection "upgrade";
echo  ==============================================================
echo.
pause
exit /b 0

rem ===========================================================================
rem  子过程
rem ===========================================================================

:abort
echo  --------------------------------------------------------------
echo  部署已中止，请先按上面的提示处理后再重试。
echo.
pause
exit /b 1

rem 生产配置预检：只提示不阻断。
rem 真正的强校验在应用启动时执行（CAB_ENV=production 不满足会拒绝启动），
rem 这里提前把问题暴露出来，避免用户对着一个"莫名起不来"的窗口排查。
:check_prod
set "_ENVFILE=backend\.env"

findstr /b /c:"CAB_ENV=production" "%_ENVFILE%" >nul 2>nul
if errorlevel 1 (
    echo        - CAB_ENV 不是 production：当前按开发模式运行，
    echo          交互式文档与调试接口仍然开放。对外发布前请改为 CAB_ENV=production。
)

findstr /c:"CAB_SECRET_KEY=change-me-in-production" "%_ENVFILE%" >nul 2>nul
if not errorlevel 1 (
    echo        - CAB_SECRET_KEY 仍为默认值：任何令牌都可被伪造。
    echo          请改为 32 位以上的随机串。
)

findstr /c:"CAB_INITIAL_ADMIN_PASSWORD=admin123" "%_ENVFILE%" >nul 2>nul
if not errorlevel 1 (
    echo        - CAB_INITIAL_ADMIN_PASSWORD 仍为默认弱口令。
    echo          请改为强口令（仅在首次创建 admin 时生效）。
)

findstr /c:"CAB_MEDIA_COOKIE_SECURE=false" "%_ENVFILE%" >nul 2>nul
if not errorlevel 1 (
    echo        - CAB_MEDIA_COOKIE_SECURE 为 false：HTTPS 部署下应置为 true。
)

echo        （以上仅为提示，不阻断本次部署）
exit /b 0

rem 检查文件或目录是否存在：  call :require "路径" "名称" "缺失时的安装指引"
:require
if exist "%~1" exit /b 0
echo  [错误] 未找到 %~2
echo         缺失项：%~1
echo         解决方法：%~3
echo.
exit /b 1

rem 端口是否处于 LISTENING：占用返回 0，空闲返回 1
:port_busy
netstat -ano | findstr /c:":%~1 " | findstr /i "LISTENING" >nul 2>nul
if errorlevel 1 exit /b 1
exit /b 0

rem 等待端口进入 LISTENING：  call :wait_port 端口 超时秒数
:wait_port
set /a _tries=0
:wait_port_loop
call :port_busy %~1
if not errorlevel 1 exit /b 0
set /a _tries+=1
if %_tries% geq %~2 exit /b 1
ping -n 2 127.0.0.1 >nul
goto :wait_port_loop
