const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('venue', {
  load: () => ipcRenderer.invoke('venue:load'),
  schedule: config => ipcRenderer.invoke('venue:schedule', config),
  notice: () => ipcRenderer.invoke('venue:notice'),
  save: config => ipcRenderer.invoke('venue:save', config),
  start: (config, mode) => ipcRenderer.invoke('venue:start', config, mode),
  stop: () => ipcRenderer.invoke('venue:stop'),
  retryLogin: () => ipcRenderer.invoke('venue:retryLogin'),
  openVenueLogin: () => ipcRenderer.invoke('venue:openVenueLogin'),
  restartDingTalk: () => ipcRenderer.invoke('venue:restartDingTalk'),
  onEvent: callback => {
    ipcRenderer.on('venue:event', (_event, value) => callback(value))
  },
})
