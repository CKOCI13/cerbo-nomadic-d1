// Rebuilds docs/installation-guide.pdf and docs/safe-test-plan.pdf from their HTML and print.css.
//
//   npm install playwright        (once; any recent version)
//   node docs/src/build.cjs       (from the project folder)
//
// The pictures in docs/images are kept as files.

const { chromium } = require('playwright');
const path = require('path');

const SRC = __dirname;
const DOCS = path.dirname(SRC);

(async () => {
  const browser = await chromium.launch();
  try {
    for (const name of ['installation-guide', 'safe-test-plan']) {
      const page = await browser.newPage();
      await page.goto('file://' + path.join(SRC, name + '.html'), { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      await page.pdf({
        path: path.join(DOCS, name + '.pdf'),
        format: 'Letter',
        printBackground: true,
        margin: { top: '0.6in', bottom: '0.7in', left: '0.6in', right: '0.6in' },
        displayHeaderFooter: true,
        headerTemplate: '<span></span>',
        footerTemplate: '<div style="width:100%;text-align:center;font-size:8px;color:#888;font-family:sans-serif">' +
          'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
      });
      console.log(name + '.pdf');
    }
  } finally {
    await browser.close();
  }
})();
