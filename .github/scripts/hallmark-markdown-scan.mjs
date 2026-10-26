#!/usr/bin/env node

/**
 * Markdown lint scanner built on hallmark (https://github.com/vweevers/hallmark).
 *
 * Mirrors the contract of audit-ci-vuln-scan.mjs so the two remediation
 * workflows stay symmetrical: scan the default branch, classify what was
 * found, dedupe against markers already present in open remediation PRs, and
 * emit a prompt + marker for the Claude remediation job.
 *
 * What makes this different from a plain `hallmark lint` step is the split
 * between the two kinds of finding, measured on this repo (2026-10-26):
 *
 *   445 findings across 44 files
 *   └─ 366 (82%) resolved mechanically by `hallmark fix`
 *   └─  79 across 19 files survive the fixer and need a judgement call:
 *        36  remark-validate-links:missing-file        (link target moved/renamed/deleted?)
 *        38  remark-common-changelog:*                 (what version/date was this release?)
 *         5  remark-lint:no-auto-link-without-protocol (is `example.com` a URL or a placeholder?)
 *
 * The fixer handles the first group, so this script runs it in place (the
 * resulting diff is exactly what we want to commit) and then re-lints: the
 * residue is, by construction, the part an AI has to reason about. Nothing is
 * inferred from a hardcoded rule list when the fixer can answer empirically.
 */

import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, realpathSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const MARKER_NAME = "hallmark-md-keys";
const MARKER_REGEX = /<!--\s*hallmark-md-keys:\s*(\[[\s\S]*?\])\s*-->/g;
const OUTPUT_DIR = process.env.HALLMARK_OUTPUT_DIR || "/tmp/hallmark-markdown";
const GITHUB_API_URL = process.env.GITHUB_API_URL || "https://api.github.com";
const GITHUB_TOKEN = process.env.GH_TOKEN || process.env.GITHUB_TOKEN || "";
const GITHUB_REPOSITORY = process.env.GITHUB_REPOSITORY || "";

// hallmark defaults to the non-recursive glob "*.md", which silently skips
// everything in subdirectories. Always ask for the whole tree.
const DEFAULT_FILES = ["**/*.md"];

// Rules that survived `hallmark fix` in the 2026-10-26 measurement. Used only
// to label findings when --fix is not run; with --fix the residue is measured.
const JUDGEMENT_RULES = new Set([
  "remark-validate-links:missing-file",
  "remark-common-changelog:release-version",
  "remark-common-changelog:release-version-link",
  "remark-common-changelog:release-date",
  "remark-common-changelog:latest-release-first",
  "remark-common-changelog:no-empty-release",
  "remark-common-changelog:title",
  "remark-lint:no-auto-link-without-protocol",
]);

// Guidance handed to Claude per rule family. Kept next to the classifier so the
// prompt cannot drift away from what the scanner actually reports.
const REMEDIATION_GUIDANCE = {
  "remark-validate-links:missing-file": [
    "A link target does not exist in the repository. Work out which of four cases you are in before editing,",
    "because the correct fix is different for each:",
    "",
    "  1. **The 'missing file' is actually a bare domain** — e.g. `firecrawl.dev`, `github.com`, `docs.stripe.org`,",
    "     `streamlit.io`. remark-validate-links resolved it as a relative path because the link has no protocol.",
    "     The fix is to add `https://`, NOT to go looking for a moved file. This is the single most common case here.",
    "  2. **Wrong relative depth** — the message often spells out the answer, e.g.",
    "     'Link to unknown file: `../CONTRIBUTING.md`. Did you mean `../../CONTRIBUTING.md`'. Take the suggestion,",
    "     but confirm the proposed path really exists (`git ls-files <path>`).",
    "  3. **The target was deleted and nothing replaced it** (a retired skill directory, an uncommitted",
    "     `notebook_files/*.png` from a notebook export, a missing `LICENSE`) — drop the link or point at what",
    "     genuinely replaced it, keeping the surrounding sentence readable.",
    "  4. **The text was never meant to be a link** — a bare filename, package name, or command. De-link it to",
    "     inline code.",
    "",
    "Never invent a plausible-looking path. If you cannot determine the target, de-link it and record the",
    "unresolved cases in the PR body.",
  ].join("\n"),
  "remark-common-changelog:*": [
    "A CHANGELOG.md release heading is not semver-valid, has no compare link, has no date, or releases are out of order.",
    "Recover the real value from git history (`git tag --list`, `git log`) or from the SDK's published versions.",
    "If a version genuinely cannot be determined, normalise the heading to the closest verifiable value rather than guessing,",
    "and list the unresolved headings in the PR body so a human can settle them.",
    "Do not reorder releases unless the dates/tags prove the current order is wrong.",
    "",
    "**Also clean up after the fixer in these two files.** `hallmark fix` has already appended link reference",
    "definitions to `apps/php-sdk/CHANGELOG.md` and `apps/rust-sdk/CHANGELOG.md`, and because the headings it",
    "was working from are not semver-valid, what it appended is malformed — labels like `[\\[1.11.0\\]]` and",
    "URLs containing a literal `[1.11.0]`. Once you have corrected the headings, delete those broken definitions",
    "and write correct compare links yourself.",
    "",
    "**Never run `hallmark fix` yourself.** It is not idempotent: a second pass re-appends those definitions",
    "and adds 34 `remark-lint:no-duplicate-definitions` findings. Lint with",
    "`node .github/scripts/hallmark-markdown-scan.mjs --no-fix` instead.",
  ].join("\n"),
  "remark-lint:no-auto-link-without-protocol": [
    "An autolink like `<example.com>` has no protocol.",
    "If it is a real URL, add `https://`. If it is an email address, use `<mailto:help@example.com>`.",
    "If it is a placeholder, a shell redirect, or pasted program output (for instance `<Response [500]>` or a",
    "Python repr), convert it to inline code so it stops being parsed as a link.",
    "Note these often only appear AFTER the mechanical fix has normalised the surrounding markup; they are still yours.",
  ].join("\n"),
};

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 1024 * 1024 * 64,
    ...options,
  });
}

/**
 * hallmark resolves its git host from the checkout's remote and throws
 * "Unable to find git host" without one. CI checkouts always have a remote,
 * but a local scratch copy may not; surface that as an actionable error.
 */
function assertGitHost() {
  const remote = run("git", ["remote", "get-url", "origin"]);
  if (remote.status !== 0 || !/github\.com/i.test(remote.stdout || "")) {
    throw new Error(
      "hallmark needs a GitHub origin remote to resolve its git host " +
        "(used for issue/commit autolinking). Run this from a GitHub checkout.",
    );
  }
}

/**
 * Refuse to run `hallmark fix` on a tree whose markdown is already modified.
 *
 * The fixer is not idempotent, and a second pass makes things measurably worse:
 *
 *   304 findings -> 79 after one pass -> 113 after two -> stable at 113
 *
 * The 34-finding regression is entirely remark-lint:no-duplicate-definitions,
 * concentrated in apps/php-sdk/CHANGELOG.md (16) and apps/rust-sdk/CHANGELOG.md
 * (18). Their release headings read "[1.11.0]", which is not semver-valid, so
 * hallmark's autolinkReferences fixer emits a malformed definition —
 * `[\[1.11.0\]]: …/releases/tag/v[1.11.0]` — and on the next run fails to
 * recognise its own output as already present, appending a second copy.
 *
 * CI always hands us a clean checkout, so this only fires when something has
 * already run the fixer. Pass --force-fix to override deliberately.
 */
function assertTreeCleanForFix() {
  const dirty = run("git", ["status", "--porcelain", "--", "*.md", "**/*.md"]);
  if (dirty.status !== 0) return; // no git, or pathspec unsupported: do not block
  const modified = (dirty.stdout || "").split("\n").map((l) => l.trim()).filter(Boolean);
  if (modified.length === 0) return;

  throw new Error(
    `Refusing to run \`hallmark fix\`: ${modified.length} markdown file(s) are already modified ` +
      `in this working tree (${modified.slice(0, 3).join(", ")}${modified.length > 3 ? ", …" : ""}).\n` +
      "A second fixer pass is known to add remark-lint:no-duplicate-definitions to the SDK " +
      "CHANGELOGs (79 findings become 113). Commit or discard the existing changes first, " +
      "or pass --force-fix if you really mean it.",
  );
}

async function loadHallmark() {
  const specifier = process.env.HALLMARK_MODULE || "hallmark";
  try {
    return await import(specifier);
  } catch (error) {
    throw new Error(
      `Could not import hallmark from "${specifier}" (${error.message}). ` +
        "Install it first, or point HALLMARK_MODULE at the installed entry.",
    );
  }
}

/** Run hallmark's lint/fix programmatically and return structured findings. */
async function runHallmark(mode, files) {
  const hallmark = await loadHallmark();
  const runner = mode === "fix" ? hallmark.fix : hallmark.lint;
  if (typeof runner !== "function") {
    throw new Error(`hallmark did not export a "${mode}" function`);
  }

  return new Promise((resolve, reject) => {
    // report:false swaps in a no-op reporter, so results come back as vfiles
    // instead of being printed for a human to read.
    runner({ files, report: false, quiet: true }, (error, result) => {
      if (error) {
        reject(error);
        return;
      }
      resolve(normaliseResult(result));
    });
  });
}

/** Flatten vfile messages into plain, serialisable findings. */
export function normaliseResult(result) {
  const findings = [];
  for (const file of result?.files ?? []) {
    const filePath = file.path || file.history?.at(-1) || "<unknown>";
    for (const message of file.messages ?? []) {
      findings.push({
        file: filePath,
        line: message.line ?? null,
        column: message.column ?? null,
        rule: message.ruleId ? `${message.source ?? "remark"}:${message.ruleId}` : message.source ?? "unknown",
        reason: message.reason ?? "",
        fatal: message.fatal === true,
      });
    }
  }
  return { code: result?.code ?? 0, findings };
}

/**
 * Stable identity for a finding, used for marker dedupe.
 *
 * Deliberately excludes line/column: the fixer reflows markdown, so a finding
 * that is still unresolved after a fix would otherwise look like a new one on
 * every run and the remediation job would open duplicate PRs forever.
 */
export function findingKey(finding) {
  return `${finding.file}|${finding.rule}|${finding.reason}`;
}

/**
 * Marker keys are deduped and sorted.
 *
 * findingKey omits line/column, so the same problem repeated across a file
 * (16 CHANGELOG headings all "must be semver-valid") collapses to one key. That
 * is intended: coverage means "this class of problem in this file is being
 * handled". Without dedupe the marker grew to ~9 kB of verbatim duplicates.
 */
export function buildMarker(keys) {
  return `<!-- ${MARKER_NAME}: ${JSON.stringify([...new Set(keys)].sort())} -->`;
}

export function classify(finding, { measuredResidue = null } = {}) {
  if (measuredResidue) {
    return measuredResidue.has(findingKey(finding)) ? "judgement" : "auto-fixable";
  }
  const family = finding.rule.startsWith("remark-common-changelog:")
    ? "remark-common-changelog:*"
    : finding.rule;
  return JUDGEMENT_RULES.has(family) || JUDGEMENT_RULES.has(finding.rule)
    ? "judgement"
    : "auto-fixable";
}

export function groupByRule(findings) {
  const groups = new Map();
  for (const finding of findings) {
    const family = finding.rule.startsWith("remark-common-changelog:")
      ? "remark-common-changelog:*"
      : finding.rule;
    if (!groups.has(family)) groups.set(family, []);
    groups.get(family).push(finding);
  }
  return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
}

/** Files changed in a PR, filtered to markdown. Used by the PR gate. */
export function changedMarkdownFiles(baseRef, headRef = "HEAD") {
  const mergeBase = run("git", ["merge-base", baseRef, headRef]);
  const ref = mergeBase.status === 0 ? (mergeBase.stdout || "").trim() : baseRef;
  const diff = run("git", ["diff", "--name-only", "--diff-filter=ACMR", ref, headRef]);
  if (diff.status !== 0) {
    throw new Error(`git diff failed: ${(diff.stderr || "").trim()}`);
  }
  return (diff.stdout || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.endsWith(".md") || line.endsWith(".markdown"));
}

// --- open-PR dedupe (same trust rules as audit-ci-vuln-scan.mjs) ------------

export function isTrustedCoveragePull(pull, repository = GITHUB_REPOSITORY) {
  const fullName = pull?.head?.repo?.full_name;
  if (typeof fullName !== "string" || !repository) return false;
  return fullName.toLowerCase() === repository.toLowerCase();
}

export function extractCoveredKeys(pulls, repository = GITHUB_REPOSITORY) {
  const covered = new Map();
  for (const pull of pulls ?? []) {
    // Only trust markers from same-repo PRs: a fork could otherwise plant a
    // marker in its PR body and suppress remediation of a real finding.
    if (!isTrustedCoveragePull(pull, repository)) continue;
    const body = pull.body || "";
    for (const match of body.matchAll(MARKER_REGEX)) {
      let keys;
      try {
        keys = JSON.parse(match[1]);
      } catch {
        continue; // ignore malformed markers rather than treating them as coverage
      }
      if (!Array.isArray(keys)) continue;
      for (const key of keys) {
        if (typeof key !== "string") continue;
        if (!covered.has(key)) covered.set(key, []);
        covered.get(key).push({
          number: pull.number,
          url: pull.html_url || `https://github.com/${repository}/pull/${pull.number}`,
        });
      }
    }
  }
  return covered;
}

async function fetchOpenPulls() {
  if (!GITHUB_TOKEN || !GITHUB_REPOSITORY) return [];
  const pulls = [];
  for (let page = 1; page <= 10; page += 1) {
    const url =
      `${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/pulls` +
      `?state=open&per_page=100&page=${page}`;
    const response = await fetch(url, {
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${GITHUB_TOKEN}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });
    if (!response.ok) {
      console.warn(`Could not list open pulls (HTTP ${response.status}); skipping dedupe.`);
      return pulls;
    }
    const batch = await response.json();
    if (!Array.isArray(batch) || batch.length === 0) break;
    pulls.push(...batch);
    if (batch.length < 100) break;
  }
  return pulls;
}

// --- prompt ----------------------------------------------------------------

function guidanceFor(rule) {
  const family = rule.startsWith("remark-common-changelog:") ? "remark-common-changelog:*" : rule;
  return REMEDIATION_GUIDANCE[family] || REMEDIATION_GUIDANCE[rule] || null;
}

export function buildPrompt({ unresolved, autoFixedCount, surfacedByFix = 0, marker, scope, files }) {
  const byRule = groupByRule(unresolved);
  const fileCount = new Set(unresolved.map((f) => f.file)).size;

  const lines = [
    "You are remediating Markdown style violations in the Firecrawl monorepo, detected by",
    "[hallmark](https://github.com/vweevers/hallmark) (a remark-lint based style guide).",
    "",
    "## What already happened",
    "",
    `- Scope: ${scope}`,
    `- \`hallmark fix\` has already been run in this workspace. Its diff is part of what you will commit.`,
    `  It mechanically resolved ${autoFixedCount} of the original findings.`,
    `- ${unresolved.length} finding(s) across ${fileCount} file(s) remain. Those are yours.`,
    surfacedByFix > 0
      ? `- ${surfacedByFix} of those only surfaced *after* the fixer normalised the surrounding markup` +
        " (they were not violations in the original text). Treat them exactly like the rest."
      : "- None of the remaining findings were introduced by the fixer.",
    "",
    "The fixer is deliberately not re-run for these: each one needs a judgement call about",
    "content, not formatting. Do not reformat what the fixer already settled.",
    "",
    "## Findings that need a judgement call",
    "",
  ];

  for (const [rule, findings] of byRule) {
    lines.push(`### \`${rule}\` — ${findings.length} finding(s)`, "");
    const guidance = guidanceFor(rule);
    if (guidance) {
      lines.push(guidance, "");
    }
    const perFile = new Map();
    for (const finding of findings) {
      if (!perFile.has(finding.file)) perFile.set(finding.file, []);
      perFile.get(finding.file).push(finding);
    }
    for (const [file, items] of [...perFile.entries()].sort()) {
      lines.push(`- \`${file}\``);
      for (const item of items.slice(0, 12)) {
        const at = item.line ? `L${item.line}${item.column ? `:${item.column}` : ""}` : "";
        lines.push(`  - ${at} — ${item.reason}`);
      }
      if (items.length > 12) lines.push(`  - …and ${items.length - 12} more in this file`);
    }
    lines.push("");
  }

  lines.push(
    "## Ground rules",
    "",
    "- Touch only Markdown. Never edit code, config, fixtures or lockfiles to make a lint pass.",
    "- Preserve meaning and language. Much of this repo's documentation is bilingual; do not translate,",
    "  summarise, or 'tidy' prose beyond what the specific finding requires.",
    "- Do not silence a finding by deleting the content it complains about, unless the content is genuinely dead.",
    "- Do not add hallmark ignore comments or edit `.hallmarkrc` to make findings disappear.",
    "- Verify your work: after editing, the scanner must report zero unresolved findings.",
    "  Re-run it with `node .github/scripts/hallmark-markdown-scan.mjs --no-fix`.",
    "",
    "## Pull request requirements",
    "",
    `- Open a PR against \`${process.env.GITHUB_REF_NAME || "main"}\` with a conventional-commit title`,
    "  such as `docs: resolve remaining markdown lint findings`.",
    "- In the PR body, explain what you changed per rule family, and list anything you could not resolve.",
    `- Add this hidden marker inside the PR body's \`## Summary\` section:`,
    `  ${marker}`,
    "",
    "The marker lets the next scheduled scan tell that these findings are already being handled,",
    "so it will not open a duplicate PR.",
  );

  if (files.length > 0) {
    lines.push("", "## Files in scope", "");
    for (const file of files) lines.push(`- \`${file}\``);
  }

  return `${lines.join("\n")}\n`;
}

function writeGithubOutput(name, value) {
  if (!process.env.GITHUB_OUTPUT) return;
  if (String(value).includes("\n")) {
    const delimiter = `EOF_${name}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    appendFileSync(process.env.GITHUB_OUTPUT, `${name}<<${delimiter}\n${value}\n${delimiter}\n`);
  } else {
    appendFileSync(process.env.GITHUB_OUTPUT, `${name}=${value}\n`);
  }
}

function parseArgs(argv) {
  const args = { fix: true, frail: false, forceFix: false, scope: "all", baseRef: "", files: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--fix") args.fix = true;
    else if (arg === "--force-fix") args.forceFix = true;
    else if (arg === "--no-fix") args.fix = false;
    else if (arg === "--frail") args.frail = true;
    else if (arg === "--scope") args.scope = argv[++i];
    else if (arg === "--base-ref") args.baseRef = argv[++i];
    else if (arg.startsWith("--scope=")) args.scope = arg.slice("--scope=".length);
    else if (arg.startsWith("--base-ref=")) args.baseRef = arg.slice("--base-ref=".length);
    else if (arg === "--help" || arg === "-h") args.help = true;
    else args.files = [...(args.files ?? []), arg];
  }
  return args;
}

const USAGE = `Usage: hallmark-markdown-scan.mjs [options] [file...]

Options:
  --fix            Run \`hallmark fix\` in place first, then report what survives (default)
  --no-fix         Lint only; classify findings against the measured residue rule set
  --force-fix      Run \`hallmark fix\` even if markdown is already modified
                   (a second pass adds duplicate link definitions; see the guard)
  --frail          Exit 1 when any finding remains (used by the PR gate)
  --scope <s>      "all" (default) or "changed" (only markdown changed in this PR)
  --base-ref <ref> Base ref for --scope changed (default: origin/main)
`;

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(USAGE);
    return 0;
  }

  assertGitHost();
  mkdirSync(OUTPUT_DIR, { recursive: true });

  let files = args.files ?? DEFAULT_FILES;
  let scope = args.scope;
  if (scope === "changed") {
    const baseRef = args.baseRef || process.env.BASE_REF || "origin/main";
    files = changedMarkdownFiles(baseRef);
    if (files.length === 0) {
      console.log(`No markdown changed against ${baseRef}; nothing to lint.`);
      // Emit the full output set even on the empty path. Downstream steps guard
      // on needs_ai today, but a step added later that reads `marker` or
      // `files_with_findings` would otherwise get a silent empty string.
      for (const name of [
        "has_findings",
        "needs_ai",
        "total_findings",
        "remaining_findings",
        "auto_fixed",
        "surfaced_by_fix",
        "uncovered_findings",
        "covered_findings",
        "files_with_findings",
      ]) {
        writeGithubOutput(name, name.startsWith("has_") || name === "needs_ai" ? "false" : "0");
      }
      writeGithubOutput("marker", buildMarker([]));
      writeGithubOutput("prompt", "");
      writeGithubOutput("prompt_file", "");
      return 0;
    }
    console.log(`Linting ${files.length} markdown file(s) changed against ${baseRef}.`);
  } else {
    scope = "all";
    console.log(`Linting ${files.join(", ")} across the repository.`);
  }

  const before = await runHallmark("lint", files);
  console.log(`Before fix: ${before.findings.length} finding(s), exit code ${before.code}.`);

  let after = before;
  if (args.fix && before.findings.length > 0) {
    if (!args.forceFix) assertTreeCleanForFix();
    await runHallmark("fix", files);
    after = await runHallmark("lint", files);
    console.log(`After fix:  ${after.findings.length} finding(s) survive.`);
  }

  const residueKeys = new Set(after.findings.map(findingKey));
  const beforeKeys = new Set(before.findings.map(findingKey));
  const unresolved = after.findings;
  const autoFixedCount = before.findings.filter((f) => !residueKeys.has(findingKey(f))).length;
  // hallmark fix is not purely subtractive: normalising markup can unmask rules
  // that were not evaluable before (an email autolink only becomes
  // no-auto-link-without-protocol once the surrounding list is fixed up, and a
  // CHANGELOG heading only reaches release-date once its version is semver-valid).
  // Counting these separately keeps the arithmetic honest:
  //   before - autoFixed + surfaced == after
  const surfacedByFix = after.findings.filter((f) => !beforeKeys.has(findingKey(f)));

  const classified = after.findings.map((finding) => ({
    ...finding,
    kind: classify(finding, { measuredResidue: args.fix ? residueKeys : null }),
  }));

  const pulls = await fetchOpenPulls();
  const covered = extractCoveredKeys(pulls);
  const uncovered = classified.filter((f) => !covered.has(findingKey(f)));
  const alreadyCovered = classified.filter((f) => covered.has(findingKey(f)));

  writeFileSync(
    path.join(OUTPUT_DIR, "findings-before.json"),
    `${JSON.stringify(before.findings, null, 2)}\n`,
  );
  writeFileSync(
    path.join(OUTPUT_DIR, "findings-after.json"),
    `${JSON.stringify(classified, null, 2)}\n`,
  );

  const marker = buildMarker(uncovered.map(findingKey));
  const prompt = buildPrompt({
    unresolved: uncovered,
    autoFixedCount,
    surfacedByFix: surfacedByFix.length,
    marker,
    scope: scope === "changed" ? `markdown changed against ${args.baseRef || "origin/main"}` : "all markdown in the repository",
    files: [...new Set(uncovered.map((f) => f.file))].sort(),
  });
  const promptPath = path.join(OUTPUT_DIR, "claude-prompt.md");
  writeFileSync(promptPath, prompt);

  writeGithubOutput("has_findings", before.findings.length > 0 ? "true" : "false");
  writeGithubOutput("needs_ai", uncovered.length > 0 ? "true" : "false");
  writeGithubOutput("total_findings", String(before.findings.length));
  writeGithubOutput("remaining_findings", String(unresolved.length));
  writeGithubOutput("auto_fixed", String(autoFixedCount));
  writeGithubOutput("surfaced_by_fix", String(surfacedByFix.length));
  writeGithubOutput("uncovered_findings", String(uncovered.length));
  writeGithubOutput("covered_findings", String(alreadyCovered.length));
  writeGithubOutput("files_with_findings", String(new Set(before.findings.map((f) => f.file)).size));
  writeGithubOutput("prompt", prompt);
  writeGithubOutput("prompt_file", promptPath);
  writeGithubOutput("marker", marker);

  console.log("");
  console.log(`  total findings      : ${before.findings.length}`);
  console.log(`  resolved by fixer   : ${autoFixedCount}`);
  console.log(`  surfaced by fixer   : ${surfacedByFix.length}`);
  console.log(`  needing judgement   : ${unresolved.length}`);
  console.log(`  already covered by an open PR marker: ${alreadyCovered.length}`);
  console.log(`  to remediate now    : ${uncovered.length}`);
  console.log(`  prompt              : ${promptPath}`);

  if (args.frail && (unresolved.length > 0 || before.findings.length > 0)) {
    return 1;
  }
  return 0;
}

function isCurrentEntry() {
  const entry = process.argv[1];
  if (!entry) return false;
  return import.meta.url === pathToFileURL(realpathSync(path.resolve(entry))).href;
}

if (isCurrentEntry()) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      console.error(`${error.name || "Error"}: ${error.message}`);
      process.exit(2);
    });
}
