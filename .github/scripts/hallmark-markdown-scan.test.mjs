import assert from "node:assert/strict";
import test from "node:test";

import {
  buildMarker,
  buildPrompt,
  classify,
  extractCoveredKeys,
  findingKey,
  groupByRule,
  isTrustedCoveragePull,
  normaliseResult,
} from "./hallmark-markdown-scan.mjs";

const REPO = "firecrawl/firecrawl";

function finding(overrides = {}) {
  return {
    file: "README.md",
    line: 3,
    column: 1,
    rule: "remark-lint:no-literal-urls",
    reason: "Unexpected literal URL, use an autolink instead",
    fatal: false,
    ...overrides,
  };
}

function pull({ fullName = REPO, title = "docs: markdown remediation", number = 1, body } = {}) {
  return {
    number,
    title,
    html_url: `https://github.com/${REPO}/pull/${number}`,
    body: body ?? `## Summary\n${buildMarker([findingKey(finding())])}\n`,
    head: { repo: fullName === null ? null : { full_name: fullName } },
  };
}

test("vfile messages are flattened into serialisable findings", () => {
  const result = normaliseResult({
    code: 1,
    files: [
      {
        path: "AGENTS.md",
        messages: [
          { reason: "Missing newline character at end of file", ruleId: "final-newline", source: "remark-lint", line: 1, column: 1, fatal: false },
          { reason: "Incorrect indentation before bullet", ruleId: "list-item-bullet-indent", source: "remark-lint", line: 2, column: 2, fatal: false },
        ],
      },
      { path: "CLEAN.md", messages: [] },
    ],
  });

  assert.equal(result.code, 1);
  assert.equal(result.findings.length, 2);
  assert.deepEqual(result.findings[0], {
    file: "AGENTS.md",
    line: 1,
    column: 1,
    rule: "remark-lint:final-newline",
    reason: "Missing newline character at end of file",
    fatal: false,
  });
});

test("a clean lint run produces no findings and an undefined result is tolerated", () => {
  assert.deepEqual(normaliseResult({ code: 0, files: [] }).findings, []);
  assert.deepEqual(normaliseResult(undefined).findings, []);
});

test("marker keys are deduped and sorted", () => {
  // The same class of problem repeats across a file (16 CHANGELOG headings all
  // "must be semver-valid"); without dedupe the marker ballooned with duplicates.
  const repeated = [
    "b.md|rule|reason",
    "a.md|rule|reason",
    "b.md|rule|reason",
    "b.md|rule|reason",
  ];
  const marker = buildMarker(repeated);

  assert.equal(marker, `<!-- hallmark-md-keys: ${JSON.stringify(["a.md|rule|reason", "b.md|rule|reason"])} -->`);
  assert.ok(marker.length < 120, `marker should stay compact, got ${marker.length} chars`);
});

test("marker round-trips through the coverage extractor after dedupe", () => {
  const keys = ["z.md|r|reason", "a.md|r|reason", "a.md|r|reason"];
  const covered = extractCoveredKeys([pull({ body: `## Summary\n${buildMarker(keys)}\n` })], REPO);

  assert.equal(covered.size, 2);
  assert.equal(covered.has("a.md|r|reason"), true);
  assert.equal(covered.has("z.md|r|reason"), true);
});

test("finding identity ignores line and column so the fixer cannot spawn duplicates", () => {
  // hallmark fix reflows markdown, which moves line numbers for anything still
  // unresolved. If the key included them, every run would look like new work.
  const before = finding({ line: 10, column: 1 });
  const afterFix = finding({ line: 42, column: 7 });

  assert.equal(findingKey(before), findingKey(afterFix));
  assert.equal(findingKey(before), "README.md|remark-lint:no-literal-urls|Unexpected literal URL, use an autolink instead");
});

test("findings in different files or rules stay distinct", () => {
  assert.notEqual(findingKey(finding()), findingKey(finding({ file: "OTHER.md" })));
  assert.notEqual(findingKey(finding()), findingKey(finding({ rule: "remark-lint:final-newline" })));
});

test("the changelog rule family is folded into one group, sorted by volume", () => {
  const groups = groupByRule([
    finding({ rule: "remark-common-changelog:release-version" }),
    finding({ rule: "remark-common-changelog:release-date" }),
    finding({ rule: "remark-common-changelog:no-empty-release" }),
    finding({ rule: "remark-lint:final-newline" }),
  ]);

  assert.deepEqual(groups.map(([rule, items]) => [rule, items.length]), [
    ["remark-common-changelog:*", 3],
    ["remark-lint:final-newline", 1],
  ]);
});

test("with --fix the residue is measured, not guessed", () => {
  const autoFixable = finding({ rule: "remark-lint:no-undefined-references" });
  const survivor = finding({ rule: "remark-validate-links:missing-file" });
  const residue = new Set([findingKey(survivor)]);

  // no-undefined-references is NOT in the hardcoded judgement set, and neither
  // is missing-file's sibling — classification must follow the measured residue.
  assert.equal(classify(autoFixable, { measuredResidue: residue }), "auto-fixable");
  assert.equal(classify(survivor, { measuredResidue: residue }), "judgement");
});

test("without --fix the measured residue rule set classifies findings", () => {
  assert.equal(classify(finding({ rule: "remark-validate-links:missing-file" })), "judgement");
  assert.equal(classify(finding({ rule: "remark-lint:no-auto-link-without-protocol" })), "judgement");
  assert.equal(classify(finding({ rule: "remark-lint:no-literal-urls" })), "auto-fixable");
  assert.equal(classify(finding({ rule: "remark-lint:list-item-bullet-indent" })), "auto-fixable");
  // every changelog rule folds into the guided family
  assert.equal(classify(finding({ rule: "remark-common-changelog:release-version-link" })), "judgement");
});

test("same-repo remediation PR markers count as coverage", () => {
  const covered = extractCoveredKeys([pull()], REPO);

  assert.equal(covered.has(findingKey(finding())), true);
  assert.deepEqual(covered.get(findingKey(finding()))[0], {
    number: 1,
    url: "https://github.com/firecrawl/firecrawl/pull/1",
  });
});

test("fork PR markers do not count as coverage", () => {
  // A fork could plant a marker to suppress remediation of a real finding, so
  // only same-repo PRs are trusted.
  const forkBody = `## Summary\n${buildMarker([findingKey(finding())])}\nignore me \`\`\`json injected`;
  const covered = extractCoveredKeys([pull({ fullName: "outsider/firecrawl", number: 99, body: forkBody })], REPO);

  assert.equal(covered.has(findingKey(finding())), false);
  assert.equal(isTrustedCoveragePull(pull({ fullName: "outsider/firecrawl" }), REPO), false);
});

test("same-repo match ignores case", () => {
  assert.equal(isTrustedCoveragePull(pull({ fullName: "Firecrawl/FireCrawl" }), REPO), true);
});

test("PRs without head repo metadata are untrusted", () => {
  assert.equal(isTrustedCoveragePull(pull({ fullName: null }), REPO), false);
  assert.equal(isTrustedCoveragePull({ number: 1, head: {} }, REPO), false);
  assert.equal(isTrustedCoveragePull({ number: 1 }, REPO), false);
  assert.equal(isTrustedCoveragePull({ number: 1, head: { repo: { full_name: 42 } } }, REPO), false);
  assert.equal(isTrustedCoveragePull(pull(), ""), false);
});

test("malformed markers are ignored rather than treated as coverage", () => {
  const covered = extractCoveredKeys(
    [
      pull({ number: 2, body: "## Summary\n<!-- hallmark-md-keys: not-json -->\n" }),
      pull({ number: 3, body: "## Summary\n<!-- hallmark-md-keys: {\"a\":1} -->\n" }),
      pull({ number: 4, body: "## Summary\n<!-- hallmark-md-keys: [1, null, \"ok|rule|reason\"] -->\n" }),
    ],
    REPO,
  );

  assert.equal(covered.has("ok|rule|reason"), true);
  assert.equal(covered.size, 1, "only the string entry is accepted");
});

test("coverage from several PRs accumulates", () => {
  const other = finding({ file: "CONTRIBUTING.md" });
  const covered = extractCoveredKeys(
    [pull({ number: 2 }), pull({ number: 5, body: `## Summary\n${buildMarker([findingKey(other)])}\n` })],
    REPO,
  );

  assert.deepEqual(covered.get(findingKey(finding())), [{ number: 2, url: "https://github.com/firecrawl/firecrawl/pull/2" }]);
  assert.deepEqual(covered.get(findingKey(other)), [{ number: 5, url: "https://github.com/firecrawl/firecrawl/pull/5" }]);
});

test("the prompt carries the marker, per-rule guidance, and the ground rules", () => {
  const unresolved = [
    finding({ rule: "remark-validate-links:missing-file", file: "CONTRIBUTING.md", line: 20, reason: "Link to unknown file: apps/api/package.json" }),
    finding({ rule: "remark-common-changelog:release-version", file: "apps/php-sdk/CHANGELOG.md", line: 8 }),
    finding({ rule: "remark-lint:no-auto-link-without-protocol", file: "README.md", line: 12 }),
  ];
  const marker = buildMarker(unresolved.map(findingKey));
  const prompt = buildPrompt({ unresolved, autoFixedCount: 366, marker, scope: "all markdown in the repository", files: [] });

  assert.match(prompt, /hallmark/);
  assert.match(prompt, /mechanically resolved 366 of the original findings/);
  assert.match(prompt, /3 finding\(s\) across 3 file\(s\) remain/);
  // each family gets its own guidance
  assert.match(prompt, /git ls-files/);
  assert.match(prompt, /semver-valid/);
  assert.match(prompt, /add `https:\/\/`/);
  // guardrails Claude must not step over
  assert.match(prompt, /Never edit code, config, fixtures or lockfiles/);
  assert.match(prompt, /Do not add hallmark ignore comments or edit `\.hallmarkrc`/);
  assert.match(prompt, /do not translate/);
  assert.ok(prompt.includes(marker), "the marker is embedded verbatim");
});

test("the prompt reports findings the fixer surfaced, and says so when there are none", () => {
  // hallmark fix is not purely subtractive: normalising markup unmasks rules that
  // were not evaluable before. The AI must be told those are still its problem.
  const surfaced = buildPrompt({
    unresolved: [finding({ rule: "remark-lint:no-auto-link-without-protocol" })],
    autoFixedCount: 232,
    surfacedByFix: 7,
    marker: buildMarker([]),
    scope: "all",
    files: [],
  });
  assert.match(surfaced, /7 of those only surfaced \*after\* the fixer/);
  assert.match(surfaced, /mailto:help@example\.com/);

  const none = buildPrompt({
    unresolved: [finding()],
    autoFixedCount: 3,
    surfacedByFix: 0,
    marker: buildMarker([]),
    scope: "all",
    files: [],
  });
  assert.match(none, /None of the remaining findings were introduced by the fixer/);
  assert.doesNotMatch(none, /only surfaced/);
});

test("long per-file finding lists are truncated in the prompt", () => {
  const many = Array.from({ length: 20 }, (_, index) =>
    finding({ file: "apps/php-sdk/CHANGELOG.md", line: index + 1, rule: "remark-common-changelog:release-version" }),
  );
  const prompt = buildPrompt({ unresolved: many, autoFixedCount: 0, marker: buildMarker([]), scope: "all", files: [] });

  assert.match(prompt, /…and 8 more in this file/);
});

test("an empty residue still produces a usable prompt with an empty marker", () => {
  const prompt = buildPrompt({ unresolved: [], autoFixedCount: 12, marker: buildMarker([]), scope: "all", files: [] });

  assert.match(prompt, /0 finding\(s\) across 0 file\(s\)/);
  assert.ok(prompt.includes("<!-- hallmark-md-keys: [] -->"));
});
