import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const origin = loadEnv(mode, process.cwd(), 'SIFI_WEB_').SIFI_WEB_ORIGIN;
  if (origin && !/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) {
    throw new Error('SIFI_WEB_ORIGIN must be a loopback HTTP origin, e.g. http://127.0.0.1:8080');
  }
  return {
    plugins: [tailwindcss(), svelte()],
    server: {
      proxy: origin
        ? { '/api': { target: origin, changeOrigin: true, headers: { Origin: origin } } }
        : undefined,
    },
    build: {
      // Terser escapes whitespace literals, keeping generated assets diff-check clean.
      minify: 'terser',
      outDir: '../sifi_streamer/web/assets',
      emptyOutDir: false,
      rolldownOptions: {
        output: {
          entryFileNames: 'app.js',
          assetFileNames: 'app.css',
        },
      },
    },
  };
});
