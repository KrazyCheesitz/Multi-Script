const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/usr/local/bin/chromium',
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  await page.setContent(`<!doctype html>
    <style>*{display:block}.hidden{visibility:hidden}.cf-turnstile{width:300px;height:90px}</style>
    <main>
      <textarea placeholder="Ask anything…"></textarea>
      <button aria-label="Send message">Send</button>
      <button role="combobox">Direct</button>
      <div id="badge" class="g-recaptcha hidden" style="width:256px;height:60px"></div>
    </main>`);
  await page.addScriptTag({
    content: fs.readFileSync('extension/providers/arena.js', 'utf8') + '\nwindow.__P=ZSProvider;',
  });

  if (await page.evaluate(() => window.__P.captchaPresent())) {
    throw new Error('hidden reCAPTCHA badge caused a false pause');
  }
  await page.evaluate(() => {
    const challenge = document.createElement('div');
    challenge.id = 'challenge';
    challenge.className = 'cf-turnstile';
    challenge.style.cssText = 'display:block;width:300px;height:90px;position:fixed;left:20px;top:20px';
    document.body.appendChild(challenge);
  });
  await page.waitForTimeout(50);
  const first = await page.evaluate(async () => {
    const P = window.__P;
    return {
      present: P.captchaPresent(),
      state: await P.ensureComposerReady('startup'),
    };
  });
  if (!first.present || !first.state.humanVerificationRequired || first.state.ready) {
    throw new Error('visible challenge not gated ' + JSON.stringify(first));
  }

  const cleared = await page.evaluate(async () => {
    setTimeout(() => document.getElementById('challenge').remove(), 100);
    return window.__P.waitForHumanVerification(1500);
  });
  if (!cleared || await page.evaluate(() => window.__P.captchaPresent())) {
    throw new Error('manual-clear wait did not resume');
  }

  const source = fs.readFileSync('extension/providers/arena.js', 'utf8').toLowerCase();
  for (const forbidden of ['2captcha', 'capsolver', 'captcha token', 'grecaptcha.execute', 'contentwindow.postmessage']) {
    if (source.includes(forbidden)) throw new Error('bypass implementation detected: ' + forbidden);
  }
  const core = fs.readFileSync('extension/core/main.js', 'utf8');
  if (!core.includes('CAPTCHA or bot-check manually') || !core.includes('never bypasses verification challenges')) {
    throw new Error('human verification guidance missing');
  }

  console.log('PASS Arena human-verification gate: visible challenge pauses, manual clear resumes, hidden badge ignored, no bypass logic');
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
