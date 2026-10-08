const fs = require('node:fs');
const path = require('node:path');

// Copy only the three Linux x64 executables into the Python function bundle.
// npm packages include other platforms and support files that the function
// does not need.
const repo = path.join(__dirname, '..');
const vendor = path.join(repo, 'vendor', 'bin');
fs.mkdirSync(vendor, { recursive: true });
const binaries = [
  ['node_modules/node/bin/node', 'node'],
  ['node_modules/ffmpeg-static/ffmpeg', 'ffmpeg'],
  ['node_modules/ffprobe-static/bin/linux/x64/ffprobe', 'ffprobe'],
];
for (const [source, name] of binaries) {
  const from = path.join(repo, source);
  if (!fs.existsSync(from)) throw new Error(`Missing Linux media runtime: ${source}`);
  const target = path.join(vendor, name);
  fs.copyFileSync(from, target);
  fs.chmodSync(target, 0o755);
}
