import { readdir, readFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const [releaseTag, ...unexpectedArguments] = process.argv.slice(2);
const windowsStoreUrl = 'https://apps.microsoft.com/detail/9NPCD3VLZQMX';

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

function anchors(html) {
  return [...html.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/gi)].map(([, attributes, body]) => ({
    href: attributes.match(/\bhref\s*=\s*["']([^"']*)["']/i)?.[1] ?? '',
    text: visibleText(body).replace(/\s+/g, ' ').trim(),
  }));
}

async function htmlFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return htmlFiles(path);
    }
    return entry.name.endsWith('.html') ? [path] : [];
  }));
  return nested.flat();
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

  const distPath = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
  const outputPath = join(distPath, 'index.html');
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
  const windowsButtons = anchors(html).filter(({ text }) => text.includes('Download for Windows'));
  if (windowsButtons.length !== 1 || windowsButtons[0].href !== windowsStoreUrl) {
    missingChecks.push(`one Windows download button linking to ${windowsStoreUrl}`);
  }

  const forbiddenContent = [];
  // The Microsoft Store is the only supported Windows install, so no page may
  // offer a Windows executable or describe an unsigned Windows build.
  for (const path of await htmlFiles(distPath)) {
    const pageHtml = await readFile(path, 'utf8');
    const page = relative(distPath, path);
    for (const { href } of anchors(pageHtml)) {
      if (/\.exe(?:[?#]|$)/i.test(href)) {
        forbiddenContent.push(`a Windows executable link to ${href} in ${page}`);
      }
    }
    // Release notes are historical changelog text that legitimately mentions the
    // old unsigned Windows executable, so only the other pages are scanned.
    const isReleaseNote = page.startsWith(`news${sep}`);
    if (!isReleaseNote && /\b(?:unsigned|not signed)\b[^.]*\bWindows\b|\bWindows\b[^.]*\b(?:unsigned|not signed)\b/i.test(visibleText(pageHtml))) {
      forbiddenContent.push(`text about an unsigned Windows build in ${page}`);
    }
  }

  const version = releaseTag.slice(1);
  const newsHint = `Run \`python3 scripts/write_release_news.py --version ${version}\`, then rebuild the website`;
  const newsMissing = [];
  try {
    const newsHtml = await readFile(join(distPath, 'news', version, 'index.html'), 'utf8');
    if (!visibleText(newsHtml).includes(version)) {
      newsMissing.push(`visible version ${version} in website/dist/news/${version}/index.html`);
    }
  } catch {
    newsMissing.push(`website/dist/news/${version}/index.html`);
  }
  try {
    const newsIndexHtml = await readFile(join(distPath, 'news', 'index.html'), 'utf8');
    if (!anchors(newsIndexHtml).some(({ href }) => href === `/news/${version}/`)) {
      newsMissing.push(`a link to /news/${version}/ in website/dist/news/index.html`);
    }
  } catch {
    newsMissing.push('website/dist/news/index.html');
  }
  if (newsMissing.length > 0) {
    missingChecks.push(`the release news page (${newsMissing.join('; ')}). ${newsHint}`);
  }

  if (missingChecks.length > 0) {
    fail(
      `website/dist/index.html is missing ${missingChecks.join('; ')}. `
        + 'Ensure the homepage renders the tagged release links and the Microsoft Store '
        + 'link, then rebuild the website.',
    );
  }
  if (forbiddenContent.length > 0) {
    fail(
      `website/dist contains ${forbiddenContent.join('; ')}. `
        + 'Windows installs come only from the Microsoft Store.',
    );
  }

  console.log(`Release output validation passed for ${releaseTag}.`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
