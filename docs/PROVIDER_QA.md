# Provider QA — Multi-Script 4.6.0

Supported AI websites: Arena, ChatGPT, DeepSeek, Gemini, GLM, Kimi, Meta AI, Notion AI, and Qwen.

The automated matrix executes four supported command envelopes for each of 210 direct Multi-Script tools on every provider contract, then tests four namespaced MCP probes per provider. This is 7,596 parser/tool-routing envelopes, in addition to 5,000 malformed-input fuzz cases, 1,000 generic round trips, provider adapter loading, bridge handler coverage, and engine-specific tests.

Arena received a live public-page smoke test on `/text/direct`: Direct mode, composer, Max selector, file control, and send control were present. Full account-bound model execution can vary by login, quota, model, region, and live anti-abuse checks.

## CAPTCHA and bot checks

Multi-Script does not bypass or solve challenges. When Arena displays a visible Cloudflare Turnstile, hCaptcha, or reCAPTCHA challenge, Multi-Script hides its overlays, pauses startup/sending, lets the user complete the challenge, and resumes after it clears. Hidden reCAPTCHA badges do not trigger a false pause.
