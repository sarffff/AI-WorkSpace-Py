function readPackage(pkg) {
  // 允许 electron 和 esbuild 的构建脚本
  return pkg;
}

module.exports = {
  hooks: {
    readPackage
  }
}
