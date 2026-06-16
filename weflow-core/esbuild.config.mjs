import * as esbuild from 'esbuild';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isWatch = process.argv.includes('--watch');

// ============== 插件：Electron → electron-stub ==============
const electronRedirectPlugin = {
  name: 'electron-redirect',
  setup(build) {
    build.onResolve({ filter: /^electron$/ }, () => {
      return { path: path.join(__dirname, 'src', 'electron-stub.ts') };
    });
    build.onResolve({ filter: /^electron-store$/ }, () => {
      return { path: path.join(__dirname, 'src', 'electron-stub.ts'), namespace: 'stub' };
    });
  },
};

const electronStoreStubPlugin = {
  name: 'electron-store-stub',
  setup(build) {
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => {
      return { contents: 'module.exports = {};', loader: 'js' };
    });
  },
};

/** @type {esbuild.BuildOptions} */
const config = {
  entryPoints: {
    index: 'src/index.ts',
    wcdbWorker: 'src/wcdbWorker.ts',
  },
  bundle: true,
  platform: 'node',
  target: 'node18',
  outdir: 'dist',
  format: 'cjs',
  sourcemap: true,
  external: [
    'koffi',
    'fzstd',
    'jieba-wasm',
    'silk-wasm',
    'sherpa-onnx-node',
    'ffmpeg-static',
    'worker_threads',
  ],
  plugins: [electronRedirectPlugin, electronStoreStubPlugin],
  treeShaking: true,
  minify: false,
  keepNames: true,
  loader: { '.node': 'copy' },
};

if (isWatch) {
  const ctx = await esbuild.context(config);
  await ctx.watch();
  console.log('[esbuild] Watching for changes...');
} else {
  await esbuild.build(config);
  console.log('[esbuild] Build complete: dist/index.js');
}
