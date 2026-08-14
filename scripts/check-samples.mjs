import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import fg from 'fast-glob';

const ALLOWED_SDK_CALLS = new Set([
  'list_tools',
  'list_agents',
  'invoke_connection',
]);
const ALLOWED_IMPORTS = [
  'from af_sdk.fabriq_client import FabriqClient',
  'from af_sdk.dx import',
];

const files = await fg('examples/**/*.py');
const failures = [];

if (files.length === 0) failures.push('no python examples found');

try {
  execFileSync('python3', ['-m', 'compileall', '-q', 'examples'], { stdio: 'pipe' });
} catch (error) {
  failures.push(`python compile failed:\n${error.stdout?.toString() ?? ''}${error.stderr?.toString() ?? ''}`);
}

for (const file of files) {
  const source = readFileSync(file, 'utf8');
  if (!source.includes('af_sdk')) continue;

  const importsOk = ALLOWED_IMPORTS.some((allowed) => source.includes(allowed));
  if (!importsOk) failures.push(`${file}: imports af_sdk through an unapproved path`);

  for (const match of source.matchAll(/\baf\.([a-z_]+)\(/g)) {
    if (!ALLOWED_SDK_CALLS.has(match[1])) {
      failures.push(`${file}: calls af.${match[1]}() which is not in the published SDK surface`);
    }
  }
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log(`sample check passed: ${files.length} files`);
