/**
 * RomSet Verifier — Electron main process
 * Lance le backend Python (Flask) et affiche l'UI dans une fenêtre native.
 * Dialogues système pour DAT et dossier ROMs.
 */

const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

const PORT = 8080;
const HOST = '127.0.0.1';
const ROOT = __dirname;

let mainWindow = null;
let pythonProc = null;
let serverReady = false;

/**
 * Résout un interpréteur Python 3 utilisable.
 * Vérifie réellement chaque candidat (spawn --version) — pas seulement le nom.
 * @returns {{ cmd: string, args: string[], label: string } | null}
 */
function findPython() {
  const { execFileSync, spawnSync } = require('child_process');
  const isWin = process.platform === 'win32';

  // Candidats : { cmd, argsPrefix }
  const candidates = [];
  if (isWin) {
    // py -3 est le launcher officiel Windows (plus fiable que "python" qui peut être le stub Microsoft Store)
    candidates.push({ cmd: 'py', args: ['-3'] });
    candidates.push({ cmd: 'python', args: [] });
    candidates.push({ cmd: 'python3', args: [] });
    // Chemins d'installation courants
    const localApp = process.env.LOCALAPPDATA || '';
    const programFiles = process.env.ProgramFiles || 'C:\\\\Program Files';
    const programFilesX86 = process.env['ProgramFiles(x86)'] || 'C:\\\\Program Files (x86)';
    const home = require('os').homedir();
    const extraPaths = [
      path.join(localApp, 'Programs', 'Python'),
      path.join(home, 'AppData', 'Local', 'Programs', 'Python'),
      path.join(programFiles, 'Python312'),
      path.join(programFiles, 'Python311'),
      path.join(programFiles, 'Python310'),
      path.join(programFilesX86, 'Python312'),
    ];
    for (const base of extraPaths) {
      if (!base || !fs.existsSync(base)) continue;
      try {
        // base peut être un dossier version (Python312) ou parent de Python3x
        const direct = path.join(base, 'python.exe');
        if (fs.existsSync(direct)) {
          candidates.push({ cmd: direct, args: [] });
        }
        for (const ent of fs.readdirSync(base)) {
          const exe = path.join(base, ent, 'python.exe');
          if (fs.existsSync(exe)) candidates.push({ cmd: exe, args: [] });
        }
      } catch (_) { /* ignore */ }
    }
  } else {
    candidates.push({ cmd: 'python3', args: [] });
    candidates.push({ cmd: 'python', args: [] });
    for (const p of ['/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3']) {
      if (fs.existsSync(p)) candidates.push({ cmd: p, args: [] });
    }
  }

  const seen = new Set();
  for (const c of candidates) {
    const key = c.cmd + ' ' + c.args.join(' ');
    if (seen.has(key)) continue;
    seen.add(key);

    try {
      // Vérifier que la commande existe + répond
      const check = spawnSync(c.cmd, [...c.args, '--version'], {
        encoding: 'utf8',
        timeout: 5000,
        windowsHide: true,
        env: process.env,
      });
      if (check.error) continue;
      if (check.status !== 0) continue;
      const out = ((check.stdout || '') + (check.stderr || '')).trim();
      // Accepter Python 3.x uniquement
      const m = out.match(/Python\s+(\d+)\.(\d+)/i);
      if (!m || parseInt(m[1], 10) < 3) continue;
      const label = `${c.cmd}${c.args.length ? ' ' + c.args.join(' ') : ''} (${out})`;
      console.log('[findPython] trouvé:', label);
      return { cmd: c.cmd, args: c.args, label };
    } catch (_) {
      continue;
    }
  }
  return null;
}

function startPythonBackend() {
  const script = path.join(ROOT, 'rom_verifier.py');
  if (!fs.existsSync(script)) {
    console.error('rom_verifier.py introuvable:', script);
    return { ok: false, error: 'rom_verifier.py introuvable' };
  }

  const py = findPython();
  if (!py) {
    const msg = process.platform === 'win32'
      ? 'Python 3 introuvable.\nInstallez-le depuis https://www.python.org/downloads/\net cochez « Add python.exe to PATH ».\nOu utilisez le launcher : py -3'
      : 'Python 3 introuvable (python3 / python).\nInstallez Python 3 puis réessayez.';
    console.error('[findPython]', msg);
    return { ok: false, error: msg };
  }

  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: [
      process.env.PYTHONPATH || '',
      path.join(require('os').homedir(), '.local', 'lib', 'python3.12', 'site-packages'),
      path.join(require('os').homedir(), '.local', 'lib', 'python3.11', 'site-packages'),
      path.join(require('os').homedir(), '.local', 'lib', 'python3.10', 'site-packages'),
    ].filter(Boolean).join(path.delimiter),
  };

  const args = [...py.args, script];
  console.log(`Démarrage backend: ${py.label}`);
  console.log(`  → ${py.cmd} ${args.join(' ')}`);

  pythonProc = spawn(py.cmd, args, {
    cwd: ROOT,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  pythonProc.on('error', (err) => {
    console.error('[py] spawn error:', err.message);
    pythonProc = null;
  });

  pythonProc.stdout.on('data', (data) => {
    const s = data.toString();
    process.stdout.write(`[py] ${s}`);
    if (s.includes('Running on') || s.includes('RomSet Verifier')) {
      serverReady = true;
    }
  });
  pythonProc.stderr.on('data', (data) => {
    process.stderr.write(`[py] ${data}`);
    if (data.toString().includes('Running on')) serverReady = true;
  });
  pythonProc.on('exit', (code) => {
    console.log(`Backend Python terminé (code ${code})`);
    pythonProc = null;
  });

  return { ok: true, python: py.label };
}

function waitForServer(timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tryOnce = () => {
      const req = http.get(`http://${HOST}:${PORT}/`, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() - start > timeoutMs) {
          reject(new Error('Le serveur Python ne répond pas'));
        } else {
          setTimeout(tryOnce, 300);
        }
      });
    };
    tryOnce();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: 'RomSet Verifier',
    backgroundColor: '#0d1117',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(ROOT, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.loadURL(`http://${HOST}:${PORT}/`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/* ---------- Dialogues natifs ---------- */

ipcMain.handle('dialog:openDat', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Choisir un fichier DAT / XML',
    defaultPath: path.join(ROOT, 'dat'),
    filters: [
      { name: 'DAT / XML', extensions: ['dat', 'xml', 'DAT', 'XML'] },
      { name: 'Tous les fichiers', extensions: ['*'] },
    ],
    properties: ['openFile'],
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle('dialog:openRomsFolder', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Choisir le dossier des ROMs',
    defaultPath: path.join(ROOT, 'roms'),
    properties: ['openDirectory', 'createDirectory'],
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle('app:quit', () => {
  if (pythonProc) {
    try { pythonProc.kill(); } catch (e) {}
    pythonProc = null;
  }
  app.quit();
  return true;
});

ipcMain.handle('app:getPaths', () => ({
  root: ROOT,
  dat: path.join(ROOT, 'dat'),
  roms: path.join(ROOT, 'roms'),
}));

/* ---------- Cycle de vie ---------- */

// Environnements restreints (CI / sandbox)
if (process.env.ELECTRON_DISABLE_SANDBOX || process.platform === 'linux') {
  app.commandLine.appendSwitch('no-sandbox');
  app.commandLine.appendSwitch('disable-gpu-sandbox');
}

app.whenReady().then(async () => {
  const started = startPythonBackend();
  if (!started || !started.ok) {
    dialog.showErrorBox(
      'RomSet Verifier',
      (started && started.error)
        ? started.error
        : 'Impossible de trouver Python 3.'
    );
    app.quit();
    return;
  }
  try {
    await waitForServer();
    createWindow();
  } catch (e) {
    console.error(e);
    dialog.showErrorBox(
      'RomSet Verifier',
      'Impossible de démarrer le backend Python.\n\nPython trouvé, mais le serveur ne répond pas.\n\nVérifiez Flask et lxml :\n  pip install flask lxml\n\n' + e.message
    );
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (pythonProc) {
    pythonProc.kill();
    pythonProc = null;
  }
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (pythonProc) {
    pythonProc.kill();
    pythonProc = null;
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
