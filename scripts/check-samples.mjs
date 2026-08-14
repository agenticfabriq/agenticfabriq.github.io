import { readFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import fg from 'fast-glob';

const ALLOWED_SDK_CALLS = new Set([
  'list_tools',
  'list_agents',
  'invoke_connection',
]);
const FABRIQ_IMPORT = 'from af_sdk.fabriq_client import FabriqClient';
const DX_IMPORT_RE = /from af_sdk\.dx import\s*(\([\s\S]*?\)|[^\n]+)/g;
const ALLOWED_DX_NAMES = new Set(['ToolFabric', 'AgentFabric', 'Agent', 'tool']);
const FABRIQ_BINDING_RE = /async\s+with\s+FabriqClient\([\s\S]*?\)\s+as\s+(\w+)\s*:/g;

const files = await fg('examples/**/*.py');
const failures = [];

if (files.length === 0) failures.push('no python examples found');

// Keep compileall's bytecode cache out of the source tree entirely, rather
// than relying solely on .gitignore to hide it after the fact.
const pycacheDir = mkdtempSync(join(tmpdir(), 'af-sdk-pycache-'));

try {
  execFileSync('python3', ['-m', 'compileall', '-q', 'examples'], {
    stdio: 'pipe',
    env: { ...process.env, PYTHONPYCACHEPREFIX: pycacheDir },
  });
} catch (error) {
  failures.push(`python compile failed:\n${error.stdout?.toString() ?? ''}${error.stderr?.toString() ?? ''}`);
}

for (const file of files) {
  const source = readFileSync(file, 'utf8');
  if (!source.includes('af_sdk')) continue;

  const hasFabriqImport = source.includes(FABRIQ_IMPORT);
  const dxImportMatches = [...source.matchAll(DX_IMPORT_RE)];
  const hasDxImport = dxImportMatches.length > 0;

  if (!hasFabriqImport && !hasDxImport) {
    failures.push(`${file}: imports af_sdk through an unapproved path`);
  }

  for (const match of dxImportMatches) {
    const raw = match[1].trim();
    const inner = raw.startsWith('(') && raw.endsWith(')') ? raw.slice(1, -1) : raw;
    const names = inner
      .split(',')
      .map((name) => name.trim())
      .filter(Boolean)
      .map((name) => name.split(/\s+as\s+/)[0].trim());
    for (const name of names) {
      if (!ALLOWED_DX_NAMES.has(name)) {
        failures.push(`${file}: imports af_sdk.dx name '${name}' which is not in the published SDK surface`);
      }
    }
  }

  // The SDK client isn't always bound to a variable literally named `af` —
  // find every name it's actually bound to via `async with FabriqClient(...) as <name>:`,
  // falling back to `af` only if the file imports FabriqClient but never binds it that way.
  const bindings = new Set([...source.matchAll(FABRIQ_BINDING_RE)].map((match) => match[1]));
  if (bindings.size === 0 && hasFabriqImport) bindings.add('af');

  for (const name of bindings) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const callRe = new RegExp(`\\b${escaped}\\.([a-z_]+)\\(`, 'g');
    for (const match of source.matchAll(callRe)) {
      if (!ALLOWED_SDK_CALLS.has(match[1])) {
        failures.push(`${file}: calls ${name}.${match[1]}() which is not in the published SDK surface`);
      }
    }
  }
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log(`sample check passed: ${files.length} files`);
