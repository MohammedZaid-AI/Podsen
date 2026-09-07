// Flat append-only event log. Inspect with: cat $DATA_DIR/events.jsonl  (or GET /admin?key=)
import { appendFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import process from 'node:process';

const DIR = process.env.DATA_DIR || './data';
mkdirSync(DIR, { recursive: true });

export function append(file, obj) {
  appendFileSync(join(DIR, file), JSON.stringify({ ts: new Date().toISOString(), ...obj }) + '\n');
}

export function readAll(file) {
  const p = join(DIR, file);
  if (!existsSync(p)) return [];
  return readFileSync(p, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}
