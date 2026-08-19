import { readFileSync, mkdtempSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
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

// --- import smoke-check -----------------------------------------------------
//
// `compileall` above is syntax-only. It says nothing about whether the samples
// still agree with each other, and several of them are coupled: within one
// example directory a module imports names from a sibling
// (`open_pull_request` <- `mint_installation_token`, `rotate_with_overlap` <-
// `issuing_broker`, all three framework tools <- `channel_api`, most of
// `fail-runaway-loops` <- `fake_provider` and `guardrails`, and
// `delegated_agent` <- `intersection` and `obo_exchange`). Rename or drop an
// exported name on one side and every file still compiles, so the whole verify
// run stays green while the published sample is broken for anyone who runs it.
//
// So import every module, one subprocess per module, with its own directory on
// `sys.path` (that is how a reader runs them). A module that imports cleanly
// proves the names its siblings publish are the names it asks for.
//
// A missing third-party package is a property of this machine, not of the
// sample, so it is reported as a skip — unless the missing module is a sibling
// `.py` file in the same example directory, which means a file was renamed or
// deleted and is a real failure. Everything else that `ImportError` or
// `AttributeError` can express is a failure.
//
// Importing must not *run* anything, and almost every sample cooperates by
// guarding its entry point with `if __name__ == "__main__":` — so importing it
// needs no credential and reaches no gateway. The exceptions are named below
// and skipped by name. Never widen the classification above to accommodate a
// new one; add it here with the reason instead, so the skip is visible in the
// run output and countable in review.
// The external distributions the samples are allowed to depend on. A missing
// module outside this set is never a "dependency isn't installed" excuse: it
// means a sample imports something that exists nowhere, which is what a renamed
// or deleted sibling looks like once the file is gone and the directory listing
// no longer knows it was ever there. Adding a dependency means adding it here,
// in review, on purpose.
const EXTERNAL_PACKAGES = [
  'af_sdk',
  'agents',
  'crewai',
  'cryptography',
  'httpx',
  'jwt',
  'langchain',
  'langgraph',
  'psycopg',
  'pydantic',
  'requests',
];

const IMPORT_SKIP = new Map([
  [
    'examples/connect-slack/verify_signature.py',
    'reads os.environ["SLACK_SIGNING_SECRET"] at module scope, so importing it ' +
      'demands a real signing secret. Nothing imports it — it is a standalone ' +
      'runnable server — so skipping it costs no coupling coverage.',
  ],
]);

const IMPORT_PROBE = `
import importlib, importlib.util, json, os, sys

directory, module, external = sys.argv[1], sys.argv[2], set(json.loads(sys.argv[3]))
sys.path.insert(0, directory)
siblings = {name[:-3] for name in os.listdir(directory) if name.endswith('.py')}

def emit(**payload):
    print(json.dumps(payload))
    raise SystemExit(0)

try:
    importlib.import_module(module)
except ModuleNotFoundError as error:
    missing = error.name or ''
    root = missing.split('.')[0]
    if root in siblings or root not in external:
        emit(status='fail', detail='%s: %s' % (type(error).__name__, error))
    try:
        installed = importlib.util.find_spec(root) is not None
    except (ImportError, ValueError):
        installed = False
    if installed and missing != root:
        # Distinguish "absent from this machine" from "present but a different
        # shape" so the second never hides behind the first in the run output.
        emit(
            status='skip',
            detail="package '%s' is installed here but exposes no '%s'" % (root, missing),
        )
    emit(status='skip', detail="third-party package '%s' is not installed here" % root)
except ImportError as error:
    emit(status='fail', detail='%s: %s' % (type(error).__name__, error))
except AttributeError as error:
    emit(status='fail', detail='AttributeError: %s' % error)
emit(status='ok', detail='')
`;

const importSkipped = [];
let importChecked = 0;

for (const file of files) {
  const directory = dirname(file);
  const module = basename(file, '.py');

  const skipReason = IMPORT_SKIP.get(file);
  if (skipReason) {
    importSkipped.push(`${file} (${skipReason})`);
    continue;
  }

  let raw;
  try {
    raw = execFileSync('python3', ['-c', IMPORT_PROBE, directory, module, JSON.stringify(EXTERNAL_PACKAGES)], {
      stdio: 'pipe',
      env: {
        ...process.env,
        PYTHONPYCACHEPREFIX: pycacheDir,
        PYTHONDONTWRITEBYTECODE: '1',
      },
    }).toString();
  } catch (error) {
    const output = `${error.stdout?.toString() ?? ''}${error.stderr?.toString() ?? ''}`.trim();
    failures.push(
      `${file}: importing it raised or exited non-zero, so it has an import-time side effect. ` +
        `Move the work under \`if __name__ == "__main__":\`, or add it to IMPORT_SKIP with the reason.\n${output}`,
    );
    continue;
  }

  const result = JSON.parse(raw.trim().split('\n').pop());
  if (result.status === 'fail') {
    failures.push(`${file}: import failed — ${result.detail}`);
  } else if (result.status === 'skip') {
    importSkipped.push(`${file} (${result.detail})`);
  } else {
    importChecked += 1;
  }
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
console.log(`sample check passed: ${files.length} files, ${importChecked} imported`);
if (importSkipped.length) {
  console.log(`import check skipped ${importSkipped.length}:\n  ${importSkipped.join('\n  ')}`);
}
