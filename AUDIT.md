# The Engine Audit

One command runs every check built during the October 2026 audit and writes a
single report. Ask Claude any time:

> **"Run the full audit"**, or **"Run the quick audit"**, or
> **"Run the audit's browser checks"** (any stage by name, below).

Claude runs `python3 tools/audit/run_audit.py` with the matching mode and
reports what passed, what failed, and fixes failures with your go-ahead. The
latest report is saved as `tools/audit/last-report.md`.

| Mode | Stages | About |
|---|---|---|
| **quick** | tests, screens, deps, crashes | about 25 minutes |
| **full** | quick + perf + browser | about 30 minutes |

### Running it on GitHub

The repository's **Actions** tab has **Engine audit**: press **Run workflow** and choose full or quick. It runs on
GitHub's computers against its own copy of the code and its own test database -- never Railway or live data -- so
deploying and pushing carry on while it runs; it checks the code as it was when the run started. The report is on the
run's summary page and as a download (audit-report); a failed check makes the run red. The workflow is
`.github/workflows/audit.yml`, where a nightly schedule can be switched on.

A full run is long, so Claude runs it in stages and may report as each finishes.

---

## What each stage checks

### tests -- the automated tests (101, each with many checks)

**Money and QuickBooks**
- Every kind of posting sends the right request: expenses, card refunds (as credits), checking expenses, checking refunds (as deposits), bill payments (vendor, card, bill link), card payments (checking to card), splits (each line to its account); dates, amounts and the {Engine} memo.
- Undo deletes exactly what was posted, by its own kind -- purchase, deposit, bill payment, card payment, transfer.
- A category or vendor not in that company's QuickBooks file is refused plainly, before anything is sent.
- Card payments pair with their checking withdrawals, including both still in For Review (posted once, both cleared); near-misses explain themselves.
- Bills corrected by amount or by an Other Fees line, per the company setting; Force Match choices.

**Duplicates and two people at once**
- A re-upload adds nothing; an overlapping file adds only its new days (uploads, card pulls and Plaid share one import).
- Two posts of one charge at the same moment write once; a later post is refused.
- Two charges paying one bill at once: one pays, the other is told why.

**Company isolation and roles**
- Across every endpoint: a member of one company cannot read, export or change another's data -- by address, download pass, or naming a rule, charge or bill.
- View-only members cannot make changes; no endpoint skips sign-in.
- Multi-Company shows each person only their companies; shared rules apply only where ticked.

**Matching and automation**
- Bank Feed Rules: contains, alternatives, equals, abbreviations, card account, every amount test; Off / Fill / Fill & Add; oldest rule wins; card payments never ruled; refunds per the option.
- Automatic STH categorizing: only with all four checks (charge day, amount, record, Active status); a Strong bill match wins.
- Automatic bill matching at each level, including custom bands; never refunds, pending charges, ties, or a bill another charge wants more.
- Algorithms take effect when changed: signal weights, the date gate, band starts; STH days late, weekend rollover, amount variance.
- Strength bands: editable names, starts and colors; grading follows.
- Teams grid: venues, bank details, team names and distinctive mascots identify teams; shared and too-short claims are ignored.
- A card named by an email address finds the season ticket records carrying that address, and none when the address is shared by different holders.
- Status words: a broker's own words for an active, in-progress or inactive season ticket status are read with the built-in ones, per company, and a word cannot mean two things.
- The Record Found / Record Not Found filter lists only charges whose STH record was, or was not, found.
- Algorithms > Suggestions: the number of similar past codings, the agreement needed, the data points that make charges similar and learning from people only each change what is suggested; nonsense values are refused.
- Strengths / Records: nothing ticked lists charges with neither a bill nor an STH pill.
- Each company's address book is its own: syncing one company's Profiles / Address Book leaves every other company's untouched, and counts and the matcher's lookups are per company.
- A payment plan's charge day is read however Payment Plans saved it -- a date, the last day, or a named weekday (second Tuesday, last Friday) -- and an unreadable one never breaks the Bank Feed.
- Changing JWT_SECRET keeps the stored keys: with the old value as JWT_SECRET_PREVIOUS they are read, re-encrypted under the new secret at startup, and still read once it is removed.
- Every charge's STH answer is stored ahead of time: an import starts the background lookup for its new charges, an STH sync starts it again for every For Review charge, and it never auto-adds or posts.
- The User Guide keeps up: every tab the page has is described, and nothing removed from the engine is still described (a list of removed controls and replaced wording -- Search Everything, What Runs Automatically, the 5-minute bills run, the 21-day transfer window, "Rules Template" for Teams, the old charge-day names -- added to whenever one goes). In the app, its Match Strength table is drawn from the live bands -- names, starting points and colors.
- Rate History lists each day's CAD rate kept, and loading earlier rates adds the Bank of Canada's earlier days without changing today's rate. (The window shows the last 90 days, a year more per Load Earlier Rates, Show Fewer to shorten again, and stays open throughout.)
- Canadian flags: Canadian teams and venues flag CAD; American teams whose names contain a Canadian one (Creighton's Bluejays) do not.
- Company settings do nothing until switched on (except {Engine} and suggestions from past coding, on by default), and work once on.
- Suggestions from past coding; CC Vendors; CAD rate (daily, by charge date); card statements.
- **Every red x that deletes asks first:** each red x drawn on a grid row (a rule, a team, a band, a source, a key, a card vendor, a connector) has a click handler that shows a confirmation before anything changes. Exempt, by name in the test: the x's that clear a search or a filter, and the lines of a split being built in its dialog.
- Open Bills, against a stood-in QuickBooks: an amount-only edit keeps the bill's vendor; Adjust for CAD to USD multiplies by the rate on the bill's date; Adjust for Vendors Open adds a negative "Due from Vendors - Open" line for the balance (balance to zero); Other Fees and Vendors Open lines carry the first line's memo.
- Force Match's search reaches every column -- bill number, vendor, date as written, amount, balance and memo.
- A charge with no profile takes its card's name from the same card account's other charges, or the program's card list, when only one name goes with those last four.
- Slash pulls every category on the program's account -- card spend, and wires, ACH and fees at the account level -- leaving out only moves between the entity's own Slash accounts.
- Match Transfer pairs within 5 days; a card row left half-posted by an earlier attempt goes back to For Review and posts, and a raised error never leaves a payment out of For Review with nothing in QuickBooks.
- Removing a grid row is always a red x -- no Remove or Delete word buttons.
- Multi-Company rules: a shared rule is offered and listed only for companies the person can reach; one also used by companies they can't reach can't be changed or removed by them, and its company list names only theirs.
- The "STH is not synced yet" banner shows only on the Bank Feed of one company that has Season Ticket Records set up and none read yet -- never in Multi-Company, on the Dashboard or another screen -- and goes within seconds of the first sync finishing.
- Bank Feed Rules' Split across categories opens the rule's split window (it was taken by the Bank Feed's own).
- QuickBooks cannot be removed from Connectors: its red x is grayed out, and the request is refused for everyone, owner included.
- Every Download Template file is named for its own grid, with no date: Engine - Teams Template, Engine - Bank Feed Rules Template, Engine - Credit Card Profiles Template.
- Upload Teams From Excel, on the Dashboard and in a company: every tab with a Team column is read; a new team is added with its venues and bank details, a listed one gains them, example rows are skipped, and a row with no team name loads nothing. In a company, a Dashboard team becomes an override that starts from the Dashboard's values, and a new league is added for that company only.
- A company's teams, leagues and Payment Plans affect that company only -- another company lists none of them. A company's Payment Plans list its own teams and its overrides; a plan saved there is the company's, never shown on the Dashboard, and replaces the Dashboard's in the Bank Feed's expected amount -- while an override left blank uses the Dashboard's plan.
- A league added in a company's Leagues window is that company's own (deletable there); deleting a league is refused while a Dashboard or company team is in it.
- The Open Bills line counts the company's open bills and says when they were last read.
- An STH line's total is one season's: with two renewal years chosen and a record per season (a row or a tab per season), the charge's own season is used, not both added together. Its expected amount is the plan's share for the charge's month, and the working is given for the hover.
- A profile set by a Credit Card Profiles rule is marked with that rule.
- The Bank Feed's Suggestions lists the company's suggestions from past coding; one ticked Ignore Suggestion stays listed but is not offered (unticking uses it again), and a deleted one leaves the list and is not offered.
- Each STH renewal year's pill comes from that year's own record; a Canadian team's expected amount shows the CAD to USD rate on the charge date as a third factor.
- Renewal Years lists every league the Dashboard lists, with the company's own.
- Season Ticket Records need a name: the Name column, or both First and Last Name (an email alone is refused).

**Broker data (Airtable, Google Sheets, OneDrive)**
- Each part (Profiles / Address Book, Season Ticket Records) saves as several sources across bases and apps, and saving one part keeps the other; an incomplete source is refused in words.
- Linked Airtable columns read as the text they link to, never record ids.
- A source's "Ignore rows where [column] is [value]" skips matching rows at every sync (any case, any item of a list), and rows an earlier sync stored are removed.
- Seasons one column per season, one for all, or one row per season ("2026-2027", "2026", none for active); teams read from codes and mascots through the League column; college teams.
- Google service account and OneDrive sign-in; the header row found below a blank row; an edited spreadsheet row replaced, not duplicated; a spreadsheet-only broker counted as connected; removing an app drops only its sources.

**Data correctness**
- Date filters include their end day at month-end and year-end; totals to the cent; CAD to the cent; American dates in messages.
- Exports: file names, filters and quick search on the second sheet, Multi-Company exports, download passes, saved scores (and never posting from an export).
- Match Transfer's checking row is listed on Categorized with its card side, and counts as Payment.
- API pulls: Slash's Daily Credit Card Payment sweeps stay out (and earlier ones still in For Review are removed); a Taekus wire or ACH with no card is imported with no cardholder.
- Plaid statements: disconnecting a card clears its statement and it is not read again; each new statement keeps the two before it for the hover.
- An export's Excel icon turns until its file arrives: the download's response sets a cookie naming it (a malformed token sets nothing).
- Custom Notes go into the QuickBooks memo in brackets, before {Engine}; Update Custom Notes is a batch action.
- A broker's Season Ticket Records can say Canadian teams' costs are already in USD, and then no CAD conversion is made.
- Templates (Teams, Bank Feed Rules, Credit Card Profiles) have no Filters tab.
- An American Express card's CC Last 4/5 is its five-digit ending (from the bank detail, or the parent card's account digit before the last four); the label reads CC Last 4/5 everywhere.
- The company Teams grid's league picker lists exactly the Add / Edit Leagues window's leagues; an override team can be removed.
- Credit Card Profiles: one rule can cover several card accounts; a profile a rule set follows the rule -- a changed rule changes it, a removed rule gives the charge its own name back, and a profile someone typed in stays.
- With several renewal years chosen, the STH line gives each year its own line, oldest first, with that year's total and status.
- Uploads: the statement dates are optional (none takes the whole file; one bounds its side); a file with no transaction ids is checked for duplicates within its own card account only; a card retired in QuickBooks takes no upload.
- Undo is offered only where it can reverse QuickBooks too: never for Delete on Categorized or for an edit to a posted transaction.
- A one-time cleanup removes For Review transactions dated before 9/1/2026 in every company -- never categorized, excluded or posted ones -- and a pull does not bring them back.
- Bank Feed Rules are tried, and listed, in the order dragged; Credit Card Profiles reorder by dragging too.
- A profile typed in fills the same card's blank For Review profiles; a disconnected card's old statement is cleared when Bank / Card Accounts is opened.
- Status Words: Engine Words (every company's, editable) and each company's own words decide Renewed and Not Renewed.
- Inside a company, only the shared rules it uses are listed; changing a shared rule's companies waits for the row's Save.
- Bills carry their Class (from their lines); All Bills lists every bill, open or paid, from its own table -- never the matcher's pool -- with Class and multi-bucket Aging filters, its own count line and export.
- A past-coding suggestion ignored takes its vendor and category off the rows it filled; used again, they and the badge come back.
- User Management: the inactivity sign-out minutes are saved and used for every token; a role added on Roles can only take abilities away from its access level, and an ability switched off is refused centrally (purge, export and the rest); an owner's role is not changed there.
- Plaid asks for the bank's own descriptor; where it names the card ("XXXX3004", "Virtual Account Number 5178") that is the CC Last 4/5, never a store number.
- Renewal Years offers 2025/26-style notations, read as a broker a year behind on two-year seasons.
- Match Transfer with both sides in For Review writes the payment once, directly, and links the checking side; a QuickBooks refusal names its reason and moves neither row; another post of the same payment is waited for and its entry used. The automatic payment poster leaves both sides of a pair, and the primary account, to Match Transfer.
- Renewal Status Words: Renewed, In Progress and Not Renewed built in; the Dashboard owns the Engine Notation and colors (and can add statuses), a company adds its notation and statuses of its own; a word means one status; each word's pill takes its status's color.
- Company settings: ignoring the Dashboard's Teams drops their bank details, venues and names for that company only; opting out of Multi-Company rules removes them from its rules and its list.
- Ignore Rows Where: any number of conditions, any one skipping a row, with is / is not / is any of / is none of / is empty / is not empty and amount comparisons.
- Allow Statements (Plaid update mode) reads statements again at once and never reports a card on the same login as "another bank login"; plan day pills read as the Payment Plans window writes them (1st M, Last M, 15th, Last day).
- Scheduled jobs: a failing provider never stops the others; inside Plaid, one company never stops the next; failures are recorded by name.

### screens -- the screen code
- The code is well-formed, every screen draws, and every button the screens draw has a working handler.
- Every pop-up uses the engine's own window style -- never the browser's plain prompt, alert or confirm box.
- Every grid's Save is always shown, gray until its row changes (found on every draw).
- The buttons along the bottom of every pop-up are centered (also measured in the browser audit), and in a confirmation the red choice is on the left.

### deps -- the libraries
- Every library the engine depends on, checked against the public vulnerability database.

### crashes -- invalid input
- Every endpoint is sent missing and invalid input (bad amounts, impossible dates, malformed lists): none may crash; each must refuse clearly.

### perf -- speed at volume
- 60,000 charges, 4,500 bills and 30,000 audit entries across three companies; every screen's request timed. The Bank Feed must load within 2 seconds. Timings are listed slowest first.

### browser -- the real screens, in a real browser
- **Navigation:** every screen and sub-tab (Broker Data's seven among them), in a company, in Multi-Company and on the Dashboard -- no script errors or failed requests; the selected button blue and the rest white; a loading message on every switch; grid headers stay pinned while scrolling, with nothing above them.
- **Updating spinner:** whenever an action a person started is still working after a moment -- adding a card account, a manual pull, a save -- the circle in the middle of the screen shows Updating, and goes when it finishes. Moving between screens shows no extra spinner.
- **Hover menus:** resting on a top navigation button lists its screen's tabs, and choosing one opens the screen straight on that tab; the menu never opens on its own after a click.
- **Filters stay with their company:** changing company -- to another company or to Multi-Company -- clears every filter, and the tabs show no counts from the company before.
- **First tab:** every screen opened from the navigation lands on its first tab -- Bank Feed on For Review, QBO Sync on Bank / Card Accounts, Broker Data on Databases, Configuration on Connectors, Teams & Payment Plans on Teams, Algorithms on Signals, Tags on Canada -- however it was left on the last visit.
- **Dates:** every screen's text, tooltips and fields American style.
- **Filters:** every picker starts with nothing ticked (meaning all), and ticking one narrows; each filter changes the results, reads light blue in use and white when cleared, including inside the Filters windows; Apply closes them.
- **Quick search:** narrows each grid, and each export holds exactly the rows on screen, with the search on its second sheet.
- **Excel exports and templates:** each export (Bank Feed, QBO Sync, Audit Log, Teams, Payment Plans) and each Download Template (Teams on the Dashboard and in a company, Bank Feed Rules, Credit Card Profiles) downloads straight away -- no new window, nothing asking where to save -- and is named "Engine - company - grid - date".xlsx, a template for its own grid with no date ("Engine - Teams Template.xlsx"); never "download", an id, or another grid's name.
- **Closing windows:** every window's red X closes it -- the second time it is opened as well, after the first was closed with Escape. A closed one-off window is removed from the page, so a later copy cannot find its buttons.
- **Column widths:** on every grid, a column dragged wider keeps its width after leaving the screen and coming back -- each tab of a screen keeps its own widths -- every header fits on one line before anything is resized, and checkbox columns are as narrow as they can be.
- **Grids reach the bottom:** a grid that scrolls in its own box (Payment Plans, QBO Sync) ends at the window's bottom edge, measured from where it starts; Payment Plans' column lines run down every row.
- **Hover colors:** a navigation button, tab or hover-menu item under the pointer is light blue with dark text; only the screen and tab you are on are solid blue.
- **Hover shortcuts:** Bank Feed > Bank Feed Rules from the hover menu opens the rules without showing For Review first.
- **Row selections:** on the Teams and Payment Plans grids and the Bank Feed, a ticked row is no longer ticked after opening another tab and coming back (any tab or screen change, and changing company, clears every selection). With a quick search narrowing a grid, the header's select-all ticks only the rows showing, a shift-click range skips hidden rows, and a row the search hides is unticked -- so no batch acts on a row nobody can see.
- **Each person's own layout:** a column widened by one person is not passed to the next person signing in on the same browser, and comes back when the first signs in again.
- **Typing:** every quick search on every screen and tab is typed into a letter at a time and holds exactly what was typed -- never backwards, as when a grid redraws its own search box and puts the cursor back at the start.
- **Window sizes:** 1280x720, 1920x1080 and 1024x700 -- the header fits, no page scrolls sideways (except Bank Feed Rules and Algorithms, wide by design), button labels capitalized, American spelling.
- **Accessibility:** the standard axe checker on eight screens -- no critical or serious problems.
- **Consistency, on every grid of every screen:**
  - one header style -- white, bold, all capitals, the same size and ink;
  - every grid's columns adjustable by dragging a header's edge, and widening one column never moves another;
  - toolbar buttons right after the quick search -- none floating at the far right;
  - every quick search's clear X in red.

---

## Not automated -- worth a person's eye now and then
- A session against a **QuickBooks sandbox** company (the audit stands QuickBooks in; a sandbox confirms how QuickBooks answers).
- Exports opened in Excel, and the User Guide read against the screens after big changes.
