import type { APIRoute } from 'astro';
import packageMetadata from '../../package.json';

// The desktop app reads this file to learn about new releases. The website
// version only moves after the release DMGs exist, so it never points ahead.
export const GET: APIRoute = () =>
  new Response(JSON.stringify({ version: packageMetadata.version }), {
    headers: { 'Content-Type': 'application/json' },
  });
