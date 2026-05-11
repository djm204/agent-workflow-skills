#!/usr/bin/env node
// agent-workflow SessionStart hook
// Reads memory, git state, todo, and lessons — outputs a compact context brief
// so Claude doesn't need to burn tools to establish session context.

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const cwd = process.cwd();

function tryRead(filePath, lines = null) {
  try {
    const content = fs.readFileSync(filePath, 'utf8');
    if (lines === null) return content.trim();
    return content.trim().split('\n').slice(-lines).join('\n');
  } catch {
    return null;
  }
}

function tryExec(cmd) {
  try {
    return execSync(cmd, { cwd, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
  } catch {
    return null;
  }
}

const parts = [];

// ─── Git state ────────────────────────────────────────────────────────────────
const branch = tryExec('git branch --show-current');
const lastCommit = tryExec('git log --oneline -1');
const status = tryExec('git status --short');
if (branch || lastCommit) {
  let gitLine = 'Git:';
  if (branch) gitLine += ` [${branch}]`;
  if (lastCommit) gitLine += ` ${lastCommit}`;
  if (status) gitLine += `\n  Changes: ${status.split('\n').map(l => l.trim()).join(', ')}`;
  parts.push(gitLine);
}

// ─── Memory ──────────────────────────────────────────────────────────────────
const memoryPaths = [
  path.join(cwd, 'MEMORY.md'),
  path.join(process.env.HOME || '', '.claude', 'projects',
    cwd.replace(/\//g, '-').replace(/^-/, ''), 'memory', 'MEMORY.md'),
];
for (const p of memoryPaths) {
  const mem = tryRead(p);
  if (mem) {
    const lines = mem.split('\n').filter(l => l.startsWith('-')).slice(0, 8);
    if (lines.length > 0) {
      parts.push(`Memory index (${path.relative(process.env.HOME || '', p)}):\n  ${lines.join('\n  ')}`);
    }
    break;
  }
}

// ─── Tasks / WIP ─────────────────────────────────────────────────────────────
const todo = tryRead(path.join(cwd, 'tasks', 'todo.md'));
if (todo) {
  const inProgress = todo.split('\n').filter(l => /^\s*-\s*\[\s\]/.test(l)).slice(0, 5);
  const lastDone = todo.split('\n').filter(l => /^\s*-\s*\[x\]/i.test(l)).slice(-1);
  if (inProgress.length > 0 || lastDone.length > 0) {
    let wip = 'tasks/todo.md WIP:';
    if (lastDone.length > 0) wip += `\n  Last done: ${lastDone[0].trim()}`;
    if (inProgress.length > 0) wip += `\n  Pending:\n    ${inProgress.map(l => l.trim()).join('\n    ')}`;
    parts.push(wip);
  }
}

// ─── Lessons ─────────────────────────────────────────────────────────────────
const lessons = tryRead(path.join(cwd, 'tasks', 'lessons.md'), 20);
if (lessons) {
  const relevant = lessons.split('\n').filter(l => l.startsWith('-') || l.startsWith('#')).slice(0, 5);
  if (relevant.length > 0) {
    parts.push(`Active lessons:\n  ${relevant.join('\n  ')}`);
  }
}

// ─── Workflow rules reminder ──────────────────────────────────────────────────
parts.push(`WORKFLOW RULES (non-negotiable):
  Task start → invoke task-start skill before writing code
  Task end   → invoke task-end skill before reporting done
  ADRs       → write to docs/adr/NNN-name.md for ANY architectural decision
  Ramp-up    → keep docs/AGENT_RAMP_UP.md current after structural changes`);

process.stdout.write(parts.join('\n\n'));
