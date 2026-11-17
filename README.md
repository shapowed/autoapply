# AutoApply

Paste a job link or description and get a one-page resume tailored to that job in seconds. AutoApply can also fill out the application for you, and it keeps track of everything you've applied to.

## Install

**Mac:** unzip **AutoApply-Mac.zip** and drag **AutoApply** into Applications, then open it. The first time, macOS may say it can't verify the developer: right-click → **Open** → **Open**. On newer macOS, if there's no Open button, go to System Settings → Privacy & Security and click **Open Anyway**. Runs natively on Apple silicon (M1/M2/M3/M4) and Intel Macs. You don't need to install Python: if your Mac doesn't have it, AutoApply downloads its own checksum-verified copy. The first launch takes a few minutes, then opens. Your data lives in `~/Library/Application Support/AutoApply`. If macOS says the app is "damaged", run `xattr -dr com.apple.quarantine /Applications/AutoApply.app` in Terminal.

**Windows:** unzip **AutoApply-Windows.zip**, move the folder somewhere permanent (like Documents), and double-click **Install AutoApply**. After that, open it from your Desktop or Start menu. If SmartScreen appears: **More info** → **Run anyway**.

**Phone (iPhone or Android):** in AutoApply on your computer, go to Settings → **Use on my phone**, turn it on, scan the QR code and enter the PIN. Then add it to your home screen: on iPhone, Share → Add to Home Screen; on Android, ⋮ → Add to Home screen. The phone connects to your computer, so the computer has to be on with AutoApply open, on the same Wi-Fi. On the phone you can tailor resumes, download them and track applications. **Fill out application** opens the form on the computer's screen.

**Quit:** click **Quit AutoApply** at the bottom left. **Find your resumes:** click **Open resumes folder**.

**Linux / developers:** run `./start-linux.sh`, or `python server.py` inside a virtualenv after `pip install -r requirements.txt`. To rebuild the downloads, run `python build.py`.

## Using it

1. **First launch:** a 2-minute setup asks for an optional API key, your resume, and your answers to common application questions.
2. **New application:** paste a job link or the full job description, then click **Tailor my resume**. You get:
   - a fit score (0–100)
   - the skills you match, and the ones the job mentions that aren't on your resume
   - the tailored resume as a PDF and a Word file
   - **Fill out application** for Greenhouse, Lever and Ashby links: a browser window opens with everything filled in and your resume attached. You check it and click Submit.
3. **Find jobs:** searches the companies you list in Settings and ranks every opening by fit.
4. **My applications:** every resume you've made, with a status for each (Submitted, Interview, Rejected…).
5. **My resume:** your master copy. Click a skill to mark it "listed only" so it's never featured in your summary or bullets.

## Jobs from LinkedIn / Indeed

Click **Apply** (not Easy Apply). If the page that opens is on greenhouse.io, lever.co or ashbyhq.com, paste that link and AutoApply can fill out the form. Otherwise, paste the job description: you still get the tailored resume and apply yourself. AutoApply doesn't automate LinkedIn or Indeed because both ban accounts that use bots.

## Honesty rules (built in)

Tailored resumes are built only from your master resume. Bullets and skills get reordered, and the job's own wording is used for skills you really have. With an API key, Claude rewords the summary and bullets, and every rewrite is checked by code: any rewrite that adds a tool, number or duty that wasn't there is thrown out. Skills a job wants that you don't have are shown to you, never added.

## Updates

AutoApply updates itself. When it opens, it checks for a new version, installs it and restarts, which takes a few seconds. While it's open, a banner appears if a new version comes out. Updates only replace the app's code; your resume, settings, keys and saved resumes are never touched. You can turn automatic installs off in **Settings → Updates**.

## Privacy

Everything stays on your computer in this folder: your resume, your answers, your API key (`data/secrets.json`) and your application list. The app only listens on your own machine. Nothing is sent anywhere except the job sites you apply to, and Anthropic if you add an API key.

## Command line (optional)

`python main.py --help` has the same features as terminal commands, for power users.
