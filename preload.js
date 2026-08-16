/**
 * Preload — expose les dialogues natifs Electron de façon sécurisée.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  /** true si on tourne dans Electron */
  isElectron: true,

  /** Ouvre le dialogue natif pour choisir un fichier .dat / .xml */
  openDatFile: () => ipcRenderer.invoke('dialog:openDat'),

  /** Ouvre le dialogue natif pour choisir un dossier de ROMs */
  openRomsFolder: () => ipcRenderer.invoke('dialog:openRomsFolder'),

  /** Chemins par défaut (racine app, dat/, roms/) */
  getPaths: () => ipcRenderer.invoke('app:getPaths'),

  /** Quitte l'application (ferme fenêtre + backend) */
  quit: () => ipcRenderer.invoke('app:quit'),
});
