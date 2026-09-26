import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const [releaseTag, ...unexpectedArguments] = process.argv.slice(2);

function fail(message) {
  throw new Error(`Release output validation failed: ${message}`);
}

function visibleText(html) {
  return html
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&#x27;/gi, "'");
}

async function main() {
  if (!releaseTag || unexpectedArguments.length > 0) {
    fail(
      'expected exactly one release tag argument (for example, '
        + '`node website/scripts/check-release-output.mjs v0.3.0`).',
    );
  }

  if (!/^v\d+\.\d+\.\d+[A-Za-z0-9.+-]*$/.test(releaseTag)) {
    fail(
      `invalid release tag ${JSON.stringify(releaseTag)}; expected a tag like `
        + '`v0.3.0`.',
    );
  }

  const outputPath = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '..',
    'dist',
    'index.html',
  );
  let html;
  try {
    html = await readFile(outputPath, 'utf8');
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    fail(`could not read website/dist/index.html; run the website build first (${detail}).`);
  }

  const expectedUrls = [
    `https://github.com/andreagrandi/draftomen/releases/download/${releaseTag}/draftomen-${releaseTag}-macos-arm64.dmg`,
    `https://github.com/andreagrandi/draftomen/releases/download/${releaseTag}/draftomen-${releaseTag}-macos-x86_64.dmg`,
    `https://github.com/andreagrandi/draftomen/releases/download/${releaseTag}/draftomen-${releaseTag}-unsigned-windows.exe`,
  ];
  const missingChecks = [];
  if (!visibleText(html).includes(releaseTag)) {
    missingChecks.push(`visible release tag ${releaseTag}`);
  }
  for (const url of expectedUrls) {
    if (!html.includes(url)) {
      missingChecks.push(`exact download URL ${url}`);
    }
  }
  if (/unsigned\s*\.dmg/i.test(visibleText(html))) {
    missingChecks.push('macOS download labels without "unsigned"');
  }

  if (missingChecks.length > 0) {
    fail(
      `website/dist/index.html is missing ${missingChecks.join('; ')}. `
        + 'Ensure the homepage renders the tagged release links and rebuild the website.',
    );
  }

  console.log(`Release output validation passed for ${releaseTag}.`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
