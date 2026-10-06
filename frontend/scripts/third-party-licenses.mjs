#!/usr/bin/env node
// Copies the license files of every package the web application depends on at
// run time into <out>/frontend/, with an index.json the image's notice
// (backend/scripts/third_party_licenses.py) turns into a table. The image
// build runs it after `bun run build`:
//
//     bun scripts/third-party-licenses.mjs /app/licenses
//
// "At run time" means the closure of package.json `dependencies` (and the
// optional ones that are installed): that is what can end up in the bundle.
// devDependencies — the build and test tools — are never shipped, so they are
// left out. Resolution walks up node_modules from each package's real path,
// which works for bun's isolated layout as well as a hoisted one.

import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(import.meta.dirname, '..')
const LICENSE_NAME = /^(licen[cs]e|copying|notice|authors)([.-].*)?$/i

function resolvePackage(name, fromDir) {
  let dir = fromDir
  for (;;) {
    const candidate = path.join(dir, 'node_modules', name, 'package.json')
    if (existsSync(candidate)) return realpathSync(path.dirname(candidate))
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

function licenseOf(pkg) {
  if (typeof pkg.license === 'string') return pkg.license
  if (pkg.license && typeof pkg.license.type === 'string') return pkg.license.type
  if (Array.isArray(pkg.licenses)) return pkg.licenses.map((l) => l.type ?? l).join(' OR ')
  return 'UNKNOWN'
}

function main() {
  const out = process.argv[2]
  if (!out) {
    console.error('usage: third-party-licenses.mjs <out-dir>')
    process.exit(2)
  }
  const target = path.join(path.resolve(out), 'frontend')
  rmSync(target, { recursive: true, force: true })
  mkdirSync(target, { recursive: true })

  const app = JSON.parse(readFileSync(path.join(ROOT, 'package.json'), 'utf8'))
  const queue = Object.keys(app.dependencies ?? {}).map((name) => [name, ROOT])
  const seen = new Map()
  while (queue.length > 0) {
    const [name, fromDir] = queue.shift()
    const dir = resolvePackage(name, fromDir)
    // An optional dependency for another platform is simply not installed.
    if (dir === null || seen.has(dir)) continue
    const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'))
    const id = `${pkg.name}@${pkg.version}`
    const files = []
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isFile() || !LICENSE_NAME.test(entry.name)) continue
      const dest = path.join(target, id.replace('/', '__'), entry.name)
      mkdirSync(path.dirname(dest), { recursive: true })
      writeFileSync(dest, readFileSync(path.join(dir, entry.name)))
      files.push(path.relative(path.dirname(target), dest).split(path.sep).join('/'))
    }
    seen.set(dir, { name: pkg.name, version: pkg.version, license: licenseOf(pkg), files })
    for (const dep of [...Object.keys(pkg.dependencies ?? {}), ...Object.keys(pkg.optionalDependencies ?? {})]) {
      queue.push([dep, dir])
    }
  }

  // The same name@version reached through two paths is one entry.
  const unique = new Map()
  for (const entry of seen.values()) unique.set(`${entry.name}@${entry.version}`, entry)
  const entries = [...unique.values()].sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version))
  writeFileSync(path.join(target, 'index.json'), `${JSON.stringify(entries, null, 2)}\n`)
  const missing = entries.filter((e) => e.files.length === 0).length
  console.log(`${entries.length} frontend packages (${missing} without a license file)`)
}

main()
