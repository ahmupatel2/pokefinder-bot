async def get_pw_context():
    """Get or create a logged-in Playwright browser context."""
    global _pw_browser, _pw_context

    try:
        from playwright.async_api import async_playwright

        if _pw_context is not None:
            return _pw_context

        pw = await async_playwright().start()
        _pw_browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"]
        )
        context = await _pw_browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        )

        if TRACKALACKER_EMAIL and TRACKALACKER_PASSWORD:
            page = await context.new_page()
            try:
                await page.goto("https://www.trackalacker.com/users/sign_in", wait_until="domcontentloaded", timeout=30000)
                # Wait for React to render the email input
                await page.wait_for_selector("input[type='email'], input[name='user[email]']", timeout=15000)
                await page.fill("input[type='email']", TRACKALACKER_EMAIL)
                await page.fill("input[type='password']", TRACKALACKER_PASSWORD)
                await page.click("input[type='submit'], button[type='submit'], button:has-text('Log in')")
                await page.wait_for_load_state("networkidle", timeout=15000)

                if "sign_in" in page.url:
                    print("[PokeFinder] TrackaLacker login failed")
                else:
                    print("[PokeFinder] TrackaLacker login successful")
            except Exception as e:
                print(f"[PokeFinder] Login page error: {e}")
            finally:
                await page.close()

        _pw_context = context
        return context

    except Exception as e:
        print(f"[PokeFinder] Playwright init error: {e}")
        return None