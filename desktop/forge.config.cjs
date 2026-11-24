const { join } = require('node:path')
const { copyFile } = require('node:fs/promises')

module.exports = {
  outDir: process.env.HDU_FORGE_OUT_DIR,
  packagerConfig: {
    electronZipDir: process.env.HDU_ELECTRON_ZIP_DIR,
    name: '杭电羽毛球助手',
    executableName: '杭电羽毛球助手',
    icon: join(__dirname, '.bundle', 'app'),
    asar: true,
    extraResource: [join(__dirname, '.bundle', 'venue-engine')],
    ignore: [/^\/(?:\.bundle|out)(?:\/|$)/, /^\/(?:fixture|smoke|forge\.config)\.cjs$/, /^\/build[_.]/, /^\/使用说明\.txt$/],
  },
  makers: [{ name: '@electron-forge/maker-zip', platforms: ['win32'] }],
  hooks: {
    postPackage: async (_config, { outputPaths }) => {
      await Promise.all(outputPaths.map(folder => copyFile(join(__dirname, '使用说明.txt'), join(folder, '使用说明.txt'))))
    },
  },
}
