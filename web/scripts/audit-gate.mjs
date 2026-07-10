#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';

const BLOCKING_SEVERITIES = new Set(['high', 'critical']);

function readJson(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (error) {
    console.error(`[audit-gate] failed to read ${label} JSON at ${filePath}: ${error.message}`);
    process.exit(2);
  }
}

function normalizeId(value) {
  return String(value);
}

function isExpired(expires) {
  if (!expires) {
    return true;
  }
  const expiry = new Date(`${expires}T23:59:59.999Z`);
  return Number.isNaN(expiry.getTime()) || expiry < new Date();
}

function buildAllowlist(allowlist) {
  const allowed = new Map();
  const entries = Array.isArray(allowlist.allowed) ? allowlist.allowed : [];

  for (const entry of entries) {
    const packageName = entry.package;
    const advisoryIds = Array.isArray(entry.advisoryIds) ? entry.advisoryIds : [];
    const reason = typeof entry.reason === 'string' ? entry.reason.trim() : '';

    if (!packageName || advisoryIds.length === 0 || !reason || isExpired(entry.expires)) {
      continue;
    }

    for (const advisoryId of advisoryIds) {
      allowed.set(`${packageName}:${normalizeId(advisoryId)}`, entry);
    }
  }

  return allowed;
}

function ownAdvisories(vulnerability) {
  return (Array.isArray(vulnerability.via) ? vulnerability.via : [])
    .filter((via) => via && typeof via === 'object');
}

function isAllowed(packageName, advisory, allowed) {
  const advisoryId = advisory.source ?? advisory.url ?? advisory.title;
  if (!advisoryId) {
    return false;
  }
  return allowed.has(`${packageName}:${normalizeId(advisoryId)}`);
}

function blockingFindings(audit, allowed) {
  if (audit.error) {
    console.error(`[audit-gate] npm audit failed: ${audit.error.summary ?? audit.error.code ?? 'unknown error'}`);
    process.exit(2);
  }
  if (!audit.vulnerabilities || typeof audit.vulnerabilities !== 'object') {
    console.error('[audit-gate] npm audit report is missing the vulnerabilities object');
    process.exit(2);
  }

  const vulnerabilities = audit.vulnerabilities ?? {};
  const findings = [];
  const allowedFindings = [];

  for (const [packageName, vulnerability] of Object.entries(vulnerabilities)) {
    const packageSeverity = vulnerability?.severity;
    if (!BLOCKING_SEVERITIES.has(packageSeverity)) {
      continue;
    }

    const advisories = ownAdvisories(vulnerability)
      .filter((advisory) => BLOCKING_SEVERITIES.has(advisory.severity ?? packageSeverity));

    if (advisories.length === 0) {
      findings.push({
        packageName,
        severity: packageSeverity,
        title: `${packageName} has ${packageSeverity} vulnerability metadata without a concrete advisory`,
      });
      continue;
    }

    for (const advisory of advisories) {
      const finding = {
        packageName,
        severity: advisory.severity ?? packageSeverity,
        source: advisory.source,
        title: advisory.title ?? advisory.name ?? packageName,
        url: advisory.url,
      };
      if (isAllowed(packageName, advisory, allowed)) {
        allowedFindings.push(finding);
      } else {
        findings.push(finding);
      }
    }
  }

  return { findings, allowedFindings };
}

const [auditPath, allowlistPath] = process.argv.slice(2);
if (!auditPath || !allowlistPath) {
  console.error('Usage: node scripts/audit-gate.mjs <npm-audit.json> <audit-allowlist.json>');
  process.exit(2);
}

const audit = readJson(path.resolve(auditPath), 'npm audit');
const allowlist = readJson(path.resolve(allowlistPath), 'allowlist');
const allowed = buildAllowlist(allowlist);
const { findings, allowedFindings } = blockingFindings(audit, allowed);

for (const finding of allowedFindings) {
  console.log(
    `[audit-gate] allowed ${finding.severity} ${finding.packageName} advisory ${finding.source}: ${finding.title}`,
  );
}

if (findings.length > 0) {
  for (const finding of findings) {
    console.error(
      `[audit-gate] blocking ${finding.severity} ${finding.packageName} advisory ${finding.source ?? 'unknown'}: ${finding.title}`,
    );
    if (finding.url) {
      console.error(`[audit-gate] ${finding.url}`);
    }
  }
  process.exit(1);
}

console.log('[audit-gate] no unapproved high or critical npm audit findings');
