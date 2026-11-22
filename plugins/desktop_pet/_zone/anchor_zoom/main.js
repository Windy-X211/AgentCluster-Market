// anchor-zoom demo main
const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('fs');
const path = require('path');
const LOG = path.join(__dirname, 'main.log');
function log(s) { fs.appendFileSync(LOG, new Date().toISOString() + ' ' + s + '\n'); }

// 最顶层的异常捕获（主进程崩了也能写一条）
process.on('uncaughtException', (e) => { log('UNCAUGHT ' + e + '\n' + (e && e.stack)); });
process.on('unhandledRejection', (e) => { log('REJECT ' + e); });

// 故意不 requestSingleInstanceLock：demo 只开一个窗口，多开互抢锁会导致
// "启动即退出"。关掉 lock 后多次启动只会让旧窗口继续跑，不崩。
app.on('second-instance', (e, argv) => { log('second-instance (ignored)'); });
app.on('window-all-closed', () => { log('window-all-closed (ignored)'); });
app.on('before-quit', () => { log('before-quit'); });

let win = null;
function createWindow() {
  log('createWindow');
  try {
    win = new BrowserWindow({
      x: 240, y: 160, width: 600, height: 600,
      frame: true,
      transparent: false,
      title: 'anchor-zoom-demo',
      show: true,
      backgroundColor: '#1a1a2e',
      resizable: true,
      webPreferences: { nodeIntegration: true, contextIsolation: false, backgroundThrottling: false }
    });
    log('BrowserWindow created id=' + win.id);
    win.on('ready-to-show', () => { log('ready-to-show'); });
    win.on('show', () => log('shown'));
    win.on('closed', () => { log('closed'); win = null; });
    win.webContents.on('did-fail-load', (e, code, msg) => log('FAIL code=' + code + ' msg=' + msg));
    win.webContents.on('render-process-gone', (e, details) => log('GONE ' + JSON.stringify(details)));
    win.webContents.on('unresponsive', () => log('UNRESPONSIVE'));
    win.webContents.on('crashed', (e) => log('CRASHED'));
    win.loadFile(path.join(__dirname, 'index.html'))
       .then(() => log('loadFile OK'))
       .catch(err => log('loadFile FAIL ' + err));
  } catch (e) { log('createWindow ERR ' + e + '\n' + (e && e.stack)); }
}

app.whenReady().then(() => {
  log('whenReady');
  createWindow();
  ipcMain.on('bounds', (e, b) => {
    if (!win) { log('bounds but no win'); return; }
    try { win.setBounds({ x: Math.round(b.x), y: Math.round(b.y), width: Math.round(b.width), height: Math.round(b.height) }); }
    catch (err) { log('setBounds ERR ' + err); }
  });
  ipcMain.on('quit', () => { log('IPC quit'); try { app.quit(); } catch (e) { } });
});
