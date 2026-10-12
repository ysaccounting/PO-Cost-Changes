"""THE ENGINE USER GUIDE -- the one source for both the on-screen guide and
its PDF (tools/build_guide.py writes app/static/guide.html and
app/static/user-guide.pdf from this). Change the guide here, then rebuild.

Markup: ("h1"|"h2"|"h3", text), ("p", text), ("ul", [items]), ("ol", [items]),
("table", [headers], [[cells]...]), ("note", text). **bold** inside text.
"""

TITLE = "The Engine: User Guide"
SUBTITLE = "Card and bank transactions, matched, coded and posted to QuickBooks Online"

GUIDE = [
("h1", "1. What the Engine Does"),
("p", "The Engine brings every card and bank transaction for each of your companies into one place, "
      "works out what each one is, and posts it to that company's QuickBooks Online file. For each "
      "transaction it can:"),
("ul", [
  "**Import it** automatically from a card program (Slash, Divvy, Taekus, Global Rewards), from a bank or card "
  "connected through Plaid, or from a file you upload.",
  "**Match it to an open bill** in QuickBooks and pay that bill, scoring how confident the match is.",
  "**Suggest a vendor and category** from your Bank Feed Rules, from card-payment and season ticket "
  "holder (STH) logic, or from how people have coded the same merchant before.",
  "**Check season ticket holder charges** against the STH records kept in Airtable: that a record "
  "exists, that its status is Active, and that the charge falls on the expected day for the expected amount.",
  "**Post the result to QuickBooks**: a bill payment, an expense, or a transfer, and record every step "
  "in the Audit Log.",
]),
("p", "Nothing reaches QuickBooks unless a person posts it, or a setting or rule you have switched on "
      "says to post it automatically."),

("h1", "2. Getting Around"),
("h2", "Signing in"),
("p", "Sign in with your username and password, then the two-step code. Working steadily keeps you "
      "signed in; after a period of inactivity you are signed out and asked to sign in again."),
("h2", "The company picker"),
("p", "The picker at the top decides what the Engine shows:"),
("table", ["Choice", "What it shows"], [
  ["Dashboard", "Engine-wide setup shared by every company: companies, users, teams and payment plans, "
                "matching algorithms, tags, the engine-wide Audit Log, and this guide."],
  ["Multi-Company", "The Bank Feed, QBO Sync, Audit Log, Bank Feed Rules and Home across every company "
                         "you can access, in one view (see section 9)."],
  ["A company", "That company's Bank Feed, QBO Sync, Broker Data, Audit Log and Configuration."],
]),
("h2", "Navigation"),
("table", ["Inside a company", "Purpose"], [
  ["Home", "A landing page (its content is still being decided)."],
  ["Bank Feed", "Review, match, code and post transactions. Most of the daily work happens here."],
  ["QBO Sync", "The company's bank and card accounts, API card connections, chart of accounts, "
               "vendors, and the Deletion / Edit Scan."],
  ["Broker Data", "Where the broker's people and season ticket records come from and how they read, and the "
                  "company's own teams and payment plans."],
  ["Audit Log", "Everything that happened in this company, who did it, and when."],
  ["Configuration", "Connectors, people and settings."],
]),
("table", ["On the Dashboard", "Purpose"], [
  ["Companies", "The list of companies and their QuickBooks connections."],
  ["User Management", "Accounts, roles and the companies each person can reach."],
  ["Teams & Payment Plans", "Every team with its league, venues, bank details and payment plan."],
  ["Algorithms", "How matching is scored, and where each match strength band starts."],
  ["Tags", "Canadian venues and the CAD rate, card payment and wire patterns, virtual cards."],
  ["Audit Log", "The engine-wide log."],
  ["User Guide", "This guide, with a downloadable PDF."],
]),
("h2", "Common controls"),
("ul", [
  "**Quick search** at the top of a grid filters it as you type; on the Bank Feed it searches once you pause "
  "typing (or press Enter).",
  "**Column headers** sort the grid; drag a header's edge to resize a column. Each grid, and each tab of a screen, "
  "remembers its own widths; before any are dragged, every column is wide enough for its header on one line. "
  "The Excel icon exports the grid; it turns while the file is being made, and a message at the top says so.",
  "**Every screen opens on its first tab** when chosen from the navigation, whichever tab it was left on.",
  "**Hover menus:** rest the pointer on a navigation button (Bank Feed, QBO Sync, Broker Data, Configuration, Teams & Payment "
  "Plans, Algorithms, Tags) and its tabs are listed; choose one to open the screen straight on it.",
  "**Long dropdowns type to narrow** in the Broker Data windows (files, tabs, bases, tables and columns): "
  "click one and type part of a name, then choose with the mouse or the arrow keys and Enter.",
  "**Green buttons** add or save, **red** removes or deletes, **blue** closes or continues. In the navigation and "
  "tab rows, the screen and tab you are on are solid blue; light blue shows what is under the pointer.",
  "**Pop-up window titles** capitalize every word except small ones (and, the, of ...). In a confirmation the "
  "red choice (Delete, Reset) is on the left and the other on the right.",
  "**A row's Save** is always shown: gray while there is nothing to save, green once the row is changed. A row's "
  "**Clear** (red outline) empties the new row.",
  "**Every pop-up window closes with the red X** in its top-right corner, which also means \"no\" "
  "on a confirmation.",
  "**A red x on a grid row** (white, with a red outline) **asks first**: removing a row -- a rule, a team, a source, a key -- is confirmed in a "
  "pop-up before anything changes. The x's that clear a search or a filter, and the lines of a split being built, "
  "do not ask.",
  "**Signing out happens after 60 minutes** with no activity.",
  "**Undo** (the red-outlined icon in the Bank Feed grid's toolbar, shown when there is something to undo) "
  "reverses your last action. Hover over it to see what it will undo.",
]),
("h2", "What Undo can and can't reverse"),
("p", "Undo reverses your own most recent action -- one step, not a history -- in the company it was taken, "
      "QuickBooks included: a posting it made is removed or put back as it was."),
("table", ["Undo reverses", "Undo can't reverse"], [
  ["Add (posting a transaction), one row or Add All -- the posting is removed from QuickBooks",
   "Delete on Categorized (post the transaction again from For Review)"],
  ["Match, one row or Match All -- the payment is removed from QuickBooks", "Splits, Force Match and Match Transfer"],
  ["Exclude", "Delete Permanently (Excluded), Delete Bill and Split Bill (Open Bills)"],
  ["An edit to a For Review transaction's vendor, category, QBO memo, profile, bank detail or custom note",
   "An edit to a transaction already in QuickBooks (edit it back)"],
  ["An Open Bills change to a bill's vendor or amount", "Convert Bill Payments to Expenses"],
  ["", "Rules, profiles, teams, payment plans, suggestions, settings, uploads, syncs, pulls, and anything done "
       "by a scheduled job"],
]),
("p", "Something Undo can't reverse is changed back where it was made: Return to For Review or Delete for a "
      "posting, the grid or window it came from for everything else."),

("h1", "3. The Bank Feed"),
("h2", "Tabs"),
("table", ["Tab", "What is in it"], [
  ["For Review", "New transactions waiting for a decision. The count beside the tab is how many."],
  ["Categorized", "Transactions that have been decided and posted to QuickBooks."],
  ["Excluded", "Transactions set aside, such as duplicates or anything that should never be posted. "
               "They can be restored with Unexclude or removed with Delete Permanently."],
  ["Open Bills", "The company's unpaid bills in QuickBooks, the other side of each match. The line above the quick "
                 "search says how many are mirrored and when they were last read (hourly); **Sync From QBO** reads "
                 "them now. Its columns include **Memo** and **Class** (the Class on the bill's lines); Filters "
                 "narrow by Class and Aging, among others."],
  ["All Bills", "Every bill in QuickBooks, open or paid, in the same grid. It starts filtered to **Current** bills "
                "(Aging); clear the filter for the rest. Its line says how many are mirrored; bills changed in "
                "QuickBooks are read hourly, and **Sync From QBO** reads them all again. Only open bills are ever "
                "offered as matches."],
]),
("p", "Three more buttons sit at the end of the same row. **Bank Feed Rules** opens the rules that set vendor "
      "and category; **Profile Rules** opens the rules that set the Profile for general cards; and "
      "**Suggestions** lists the vendor and category this company's past coding suggests (below). All three "
      "keep the Bank Feed's tabs in view; clicking any tab returns to the charges."),
("h2", "Filters"),
("ul", [
  "**Companies** (Multi-Company only) narrows the view to chosen companies.",
  "**Card Accounts** narrows to chosen cards and accounts. With nothing ticked, every card is shown. "
  "Cards are grouped into Virtuals and Non-Virtuals, and can be picked by how they arrive: API, Plaid or None.",
  "**Transaction Types** narrows to charges, refunds or payments.",
  "**CC Last 4/5** is a card's last four digits -- five for an American Express card: from the bank detail or "
  "memo, or else its parent card's account digit before the last four (read from a five-digit number in the card "
  "account's name, or its Plaid connection).",
  "**Filters** opens the full set: dates, amounts, vendor, category, profile, memo, match strength, STH record, and more. "
  "**Strengths / Records:** Record Found lists charges with an STH record; Record Not Found lists those showing "
  "No Records Found; with nothing ticked, the charges with neither a bill nor an STH pill. Charges never looked "
  "up are looked up in the background as they arrive and after each STH sync, so the lists are complete. "
  "Nothing changes until you press Apply; closing the window without it leaves the filters as they were. "
  "The button turns blue while any of these is on.",
]),
("p", "Every picker starts with nothing ticked, which shows everything: tick only the ones you want. **Clear** "
      "goes back to everything. The grid reloads once, when a picker closes, rather than on every tick."),
("h2", "Custom Notes"),
("p", "The **Custom Notes** column, after QBO Memo, holds your own note on a transaction: click its pencil, type, "
      "and press Enter. A note is added to the QuickBooks memo -- and to each line's description -- in brackets, after the text and before {Engine} "
      "(a posted transaction's memo is updated when the note changes). Notes can be sorted, found with the quick "
      "search, and exported; **Update Custom Notes** in Batch Actions sets one note on every ticked row."),
("h2", "Selecting rows"),
("p", "Tick rows to act on them together. Beneath the grid, the selection is totaled separately for charges, "
      "refunds and payments. When a whole page is ticked and there are more matching rows, a blue "
      "**Select all N matching** link selects every one across all pages; once it is used, the same place "
      "offers **Deselect all**. On every grid, ticks are cleared when you move to another tab, screen or company, "
      "and with a quick search narrowing the grid, the select-all box ticks only the rows showing."),
("h2", "Action buttons"),
("p", "Each row's first column holds its action buttons. Only the actions that apply on the current tab are shown."),
("icons", [
  ("add", "#16a34a", "Add", "For Review. Posts the transaction to QuickBooks with its vendor and category."),
  ("match", "#2d6a8e", "Match", "For Review. Pays the matched bill with this charge. Shown when a bill candidate exists."),
  ("force", "#475569", "Force Match", "For Review. Choose any open bill and pay it, outside the usual date and amount limits."),
  ("back", "#0891b2", "Unexclude", "Excluded. Returns the transaction to For Review."),
  ("trash", "#dc2626", "Delete Permanently / Delete Bill", "Excluded and Open Bills. Removes it for good, after a confirmation."),
  ("split", "#d4a017", "Split Bill", "Open Bills. Divides a bill into parts, so each part can be paid by a different charge."),
]),
("h2", "The toolbar above the grid"),
("p", "The buttons above each Bank Feed grid, left to right:"),
("toolbar", [
  ("CHEV_ICON", "#6b6a65", "Hide tabs and filters", "Folds the tabs and filter row away for more room; press again to bring them back."),
  ("XLSX_ICON", "#1d7a52", "Export to Excel", "Downloads the grid -- every row matching the filters and quick search, in your column order -- with the filters listed on a second sheet."),
  ("ADJ_ICON", "#6b6a65", "Column settings", "Show, hide and reorder the columns. Widths change by dragging a header's edge."),
  ("REFRESH_ICON", "#2d6a8f", "Refresh", "Reloads the transactions and the bill count."),
  ("UNDO_ICON", "#aa3333", "Undo", "Reverses your last action. Shown only when there is something to undo; hover to see what."),
  ("deleteOne", "#aa3333", "Delete", "Deletes the selected transaction (on tabs where deleting applies)."),
  ("UPLOAD_ICON", "#15803d", "Upload", "Loads transactions from a file (see Uploading transactions)."),
  ("excludeOne", "#aa3333", "Exclude", "Moves the selected transaction to Excluded."),
  ("", "", "Quick search and its red X", "Filters the grid as you type; the X clears it."),
  ("", "", "Batch Actions", "Acts on every ticked row at once (see Batch Actions)."),
  ("", "", "Bank Feed Rules", "Opens the rules that set vendor and category."),
]),
("h2", "Badges and pills on the Bank Feed"),
("p", "Small colored labels say at a glance what the Engine found. Hover over any of them for the detail."),
("pills", [
  ("RULE", "#dbeafe", "#1e40af", "", "Vendor or Category was filled by a Bank Feed Rule. Hover to see which rule."),
  ("RULE", "#fef3c7", "#92400e", "#fcd34d", "A best guess: the bank detail is shared by more than one team, so the team was deduced rather than stated. Check it before adding."),
  ("SUGGESTION", "#dbeafe", "#1e40af", "", "Filled from past coding -- this merchant has been coded the same way at least 5 times with 90% agreement. Gone once you change the value."),
  ("DEP", "#fef3c7", "#92400e", "#fcd34d", "The category is the deposit account: this charge matches a deposit record."),
  ("PAYMENT", "#e3f2ea", "#1d7a52", "", "A card payment -- including a checking withdrawal that pairs with one."),
  ("REFUND", "#fbe9e9", "#aa3333", "", "Money back on a card or account."),
  ("STH", "#e6eefb", "#2d6a8f", "", "The season ticket holder line under a charge: the holder's name, their plan, and a green check or red X for the charge day and the installment amount."),
  ("No STH record", "#fee2e2", "#991b1b", "", "The charge names a team, but no STH record was found for the cardholder."),
  ("96%", "#97C459", "#1f1f1d", "", "The bill line under a charge: the best matching bill, its score as a % pill in its band's color, and the bill's details."),
]),
("p", "Match strength pills take their band's color (see Match Strength). The bands, their names and colors "
      "can be edited in Algorithms > Strengths."),
("p", "Three icons also say more on hover:"),
("table", ["Icon", "Where", "What it means"], [
  ["Red hand", "For Review, beside a row's action icons",
   "Another transaction scores higher on this row's best bill. Hover to see which one and its score. Matching "
   "this row first would take the bill from the stronger claimant, so check that one before you act."],
  ["Red !", "After the Card Account name",
   "Something about this transaction in QuickBooks needs a look; hover for what. Most often it was **deleted in "
   "QuickBooks** after the Engine posted it: the Deletion / Edit Scan finds that, returns it to For Review and "
   "marks it with the red ! (\"Deleted in QuickBooks — returned to For Review\"). Categorize it again when you are "
   "ready; the ! clears once it is posted again."],
  ["Notepad", "Categorized, beside the time stamp",
   "The transaction was edited after it was categorized. Hover for every later change from the Audit Log: "
   "when, who and what, newest first."],
]),
("h2", "Row actions in detail"),
("table", ["Action", "What it does"], [
  ["Add", "Posts the transaction to QuickBooks with its vendor and category."],
  ["Match", "Pays the matched bill with this charge."],
  ["Force Match", "Pays a bill you choose even when the dates or amounts are outside the usual limits. "
                  "When the amounts differ, choose: leave the bill as it is, adjust the bill to the charge "
                  "amount, or add the difference to the bill as an Other Fees line. The window shows the charge's card "
                  "account, date, bank detail, profile, amount and last four; its search reaches every column (bill "
                  "number, vendor, date, amount, balance and memo), with **Apply oldest first** and the running total "
                  "beside it."],
  ["Match Transfer", "Pairs a checking withdrawal with the card payment it paid. When one side is already in "
                     "QuickBooks, it links the other to it. When both are still in For Review, one click writes the "
                     "payment to QuickBooks once (from the card side) and clears both rows; if QuickBooks refuses, "
                     "its reason is shown and neither row moves. **Add** on one side posts only that side. The "
                     "automatic payment posting leaves both sides of a pair to Match Transfer. The two must be the same amount within 5 days of each other. On "
                     "Categorized both sides are listed, and both count as Payment. A checking withdrawal that pairs shows the PAYMENT badge; an orange ? "
                     "beside an amount explains on hover why a near match isn't offered."],
  ["Exclude", "Moves the transaction to Excluded."],
]),
("h2", "Batch Actions"),
("p", "With rows ticked, Batch Actions offers the actions that apply to the current tab:"),
("table", ["Batch action", "What it does"], [
  ["Add All", "Posts every ticked row with its vendor and category. Rows missing either are skipped and listed."],
  ["Match All", "Pays each ticked row's top bill match. Rows without a match are skipped."],
  ["Update Fields", "One window with a column each for Vendor, Category and Custom Notes. Whatever is filled in "
                    "goes on every ticked row; a field left blank keeps each row's own. **Update & Add** (For Review) "
                    "also adds them. On Categorized a bill payment keeps its bill's vendor and category."],
  ["Split Transactions", "Divides each ticked row across several categories by percentage."],
  ["Exclude", "Moves the ticked rows to Excluded."],
  ["Return to For Review", "From Categorized: reverses the posting in QuickBooks and returns the rows for another decision."],
  ["Convert Bill Payments to Expenses", "From Categorized: turns bill payments into expenses, for a bill that was the wrong match."],
  ["Unexclude / Delete / Delete Permanently", "From Excluded: restores rows, or removes them."],
]),
("p", "On **Open Bills** and **All Bills**, Batch Actions > **Update Fields** sets the Vendor and changes the Amount "
      "of every ticked bill in one window; a field left as is keeps each bill's own."),
("p", "On **Open Bills**, a bill's amount (its pencil, or the Amount column of **Update Fields** for several) can be: a new amount; "
      "increased or decreased by a fixed amount or a percentage (as a change to the amount or an Other Fees line); "
      "**Adjust for CAD to USD** -- the amount times the CAD rate on the bill's date ($100 becomes $73.51), "
      "always as a change to the amount; or "
      "**Adjust for Vendors Open** -- a negative \"Due from Vendors - Open\" line for the open balance, so it comes "
      "to zero. An Other Fees or Vendors Open line carries the first line's memo. The window shows the bill's "
      "number, vendor, date, amount and memo."),
("h2", "Match Strength"),
("p", "Every transaction with a candidate bill gets a score from 0 to 100% and a band. The % pill beside "
      "each candidate and the Match Strength column use the same colors:"),
("table", ["Band", "Starts at", "Color"], [
  ["Perfect", "100%", "Deep green"],
  ["Very Strong", "95%", "Green"],
  ["Strong", "84%", "Light green"],
  ["Moderate", "50%", "Amber"],
  ["Weak", "Below 50%", "Red"],
]),
("p", "These are the starting bands. In Algorithms > Strengths on the Dashboard you can rename bands, change "
      "where each starts and its color, add new bands and remove them. The pills, the Strengths filter and the "
      "automatic categorizing choices all follow. The lowest band always starts at 0%."),
("h2", "Season ticket holder (STH) charges"),
("p", "When a charge's bank details name a team, the Engine looks up the holder's STH record (read from "
      "the broker's Airtable, Google Sheets or OneDrive; see Broker Data in section 6) and checks four things: the record exists, its status is Active for the designated "
      "renewal year(s), the charge date falls on the plan's charge day, and the amount matches the "
      "expected installment. For Canadian teams the expected amount is converted from CAD at the rate "
      "for the charge's own date, shown as a third factor in the working -- unless the broker's Season Ticket "
      "Records say their costs are already in USD. The expected amount is the season total times the plan's percentage for the "
      "charge's month (or the total over the plan's months); hovering over **Exp Amt** shows the working. The total "
      "is one season's: where records are kept per season (a row or a tab per season), the charge's own season "
      "is used, or the newest chosen renewal year that has records. Each renewal year's pill comes from that "
      "year's own record."),
("h2", "How the expected charge amount is worked out"),
("p", "Exp Amt on the STH line is what this installment should be. Hovering over it shows the working. It is "
      "built in four steps:"),
("ol", [
  "**Season total.** The holder's seat rows for this team, added up, for one season: the charge's own season "
  "(by its date and the team's Start Month: from July, 2026 charges pay into 26/27), or, when the records carry "
  "no status for that season, the newest chosen renewal year that has records. When some rows are renewed "
  "(by the broker's Renewal Status Words), only those are added.",
  "**This month's share.** The total times the plan's percentage for the charge's month, from Teams & Payment "
  "Plans. A first charge that lands the month before the plan starts takes the first month's share. A team "
  "with no percentages splits the total evenly over the plan's months.",
  "**CAD to USD.** For a Canadian team whose costs are in Canadian dollars, the amount is then multiplied by "
  "the CAD to USD rate for the charge's own date (Tables > CAD/USD). This is skipped when the broker's Season "
  "Ticket Records say their costs are already in USD (Costs Are In).",
  "**The check.** The charge passes (green check) when it is within the tolerance set in Algorithms > Monthly "
  "Payments of that figure; the variance shows as a signed percentage.",
]),
("table", ["Example", "Working"], [
  ["US team, plan charges 10% in October", "$3,270.00 × 10% (October) = $327.00"],
  ["Even split over 10 months", "$3,270.00 ÷ 10 months = $327.00"],
  ["Canadian team, costs in CAD", "Total cost CA$5,000.00 × 10% (October) × 0.7351 (CAD → USD on 10/1/2026) = $367.55"],
]),
("p", "A refund is never compared with the plan: it shows the season account, but no expected amount, day or "
      "variance."),
("h2", "The Profile column"),
("p", "The Profile column is the person a charge belongs to. It comes from the cardholder name, or from "
      "Profile Rules for general cards used for several people; a profile set by a Profile Rule "
      "has the blue **RULE** badge after it, and hovering over it names the rule."),
("p", "Typing a profile in for a charge also fills every other For Review charge on the same card (same card "
      "account and last four) whose profile is still blank."),

("h2", "Profile Rules"),
("p", "The button at the end of the Bank Feed's tab row. Rules that set the Profile for general cards: card "
      "account -- one or several, ticked in the same picker as the Bank Feed's, from every card account in the "
      "company's QuickBooks file, connected or not -- bank detail (Contains "
      "or Any), team, and the profile, each with an optional **Rule Name** in the first column. Tick rows (or every row in the header) for **Batch Actions**, as on Bank Feed "
      "Rules: set the Card Account, Bank Detail Rule, Team or Profile of every ticked row, or delete them. The first matching row wins; drag a row by its handle at the left to "
      "change the order (saved at once), and its red x removes it at once. A new row is filled in on the grid's last row and added with its "
      "**Add** button, as on Bank Feed Rules; **Download Template** and **Upload From Excel** sit after the quick search. A changed row shows its own Save; saving also fills "
      "the profiles of charges already in For Review. A profile a rule filled in follows the rule: when the "
      "rule is changed or removed, the charge takes the new rule's profile or gets its own name back (a profile "
      "someone typed in since is left alone). Profiles are per company, so the button is not shown in "
      "Multi-Company."),
("h2", "Uploading transactions"),
("ol", [
  "Press **Upload** and choose the card or bank account the file belongs to -- only accounts in use, and "
  "active in QuickBooks, are offered.",
  "Optionally enter a start date, an end date or both -- only transactions from the start and up to the end "
  "are taken; with neither, the whole file is -- then choose the file.",
  "The Engine recognizes the exports of Divvy, EvoPay, Flex, Global Rewards, Plaid, Slash, Taekus and WEX, "
  "and the pre-formatted export. Any other CSV is read by its column names: a date, a description, and "
  "either an Amount column or separate Debit and Credit columns. A few lines above the header row are skipped.",
  "If every amount in the file has the same sign (all positive or all negative), you are asked whether "
  "they are **Money In** or **Money Out**. This is asked every time.",
]),
("p", "Uploading the same file twice is safe: transactions already loaded are skipped and reported. A card "
      "program's own transaction id is matched across every card account; a file without one is matched within "
      "the card account it is uploaded to, so the same rows uploaded to another card are that card's."),
("h2", "Bank Feed Rules"),
("p", "The **Bank Feed Rules** button opens the rules that set vendor and category. A rule has conditions "
      "(bank detail contains or equals a value, card account, amount), the vendor and category to set (or a "
      "split), and a mode:"),
("table", ["Mode", "Effect"], [
  ["Fill", "Fills in Vendor and Category on For Review. A person still posts it."],
  ["Fill & Add", "Fills them in and posts to QuickBooks automatically."],
  ["Off", "Does nothing; the rule stays in the list."],
]),
("p", "Inside a company, **All Rules** narrows the grid to **Shared** or **Individual** rules (Select All, Clear "
      "and Close, as on the Bank Feed's filters). Rules are checked top to bottom, and the first match wins, so a narrow "
      "rule placed above a broad one beats it. The **#** column numbers the rules in the order they are tried. Drag a rule by its handle at the left to change the order; it is "
      "saved at once (inside a company its own rules move; in Multi-Company, the shared ones). **Download Template** and **Upload Rules From Excel** load "
      "many rules at once. Rules never apply to card payments, which have their own logic. Shared rules, "
      "made in Multi-Company, are tried after the company's own (see section 9)."),

("h1", "4. How the Engine Decides"),
("h2", "Automatic categorizing"),
("p", "Configuration > Settings holds each company's switches. All are off unless switched on, except "
      "suggestions from past coding:"),
("table", ["Setting", "Effect"], [
  ["Append {Engine} to transactions written to QuickBooks", "Marks every transaction the Engine posts, so it can be told apart in QuickBooks."],
  ["Automatically categorize payments", "Files card payments from checking and posts them, using the program as vendor "
                                        "and the paying bank account as category."],
  ["Suggestions from past coding", "Fills Vendor and Category when people have coded a merchant the same way at "
                                   "least 5 times with 90% agreement. Never posts. On by default; untick to turn "
                                   "suggestions off for the company."],
  ["Ignore the Dashboard's Teams", "Only this company's own Teams (Broker Data > Teams) name a team in its bank "
                                   "details; the Dashboard's bank details, venues and names are not used for it."],
  ["Exclude from Multi-Company Bank Feed Rules", "No Multi-Company rule applies to (or is listed in) this company, even "
                                                 "when a rule's companies include it."],
  ["Automatically categorize matches", "Off, Strong & Above, Very Strong & Above, or Perfect. Posts a charge's "
                                       "match when it is at that level, clearly ahead of the next candidate, and no "
                                       "other charge wants the same bill as much."],
  ["Automatically categorize STH charges", "Posts an STH charge when all four STH checks pass. A charge with a Strong "
                                           "or better bill match is left for the bill instead, so it is never counted twice."],
]),
("h2", "What fills Vendor and Category"),
("p", "In order, the first that applies wins: a person's own entry; a Bank Feed Rule; card-payment logic; "
      "STH logic; and finally suggestions from past coding."),
("h2", "Suggestions from past coding"),
("p", "Only charges a person categorized count. The Engine's own automatic filing never does, so it cannot "
      "learn from itself. A suggestion is marked \"from history (6 of 6)\" and waits for a person to accept it. "
      "It is worked out afresh as charges are coded, so one that falls below the agreement needed stops being "
      "offered until it is back above it."),
("p", "**Suggestions** on the Bank Feed lists them for this company, one row per bank detail (and whatever else "
      "Algorithms > Suggestions matches on), with how many times it was coded alike. Tick **Ignore Suggestion** to "
      "stop one being offered (untick to use it again) and press the row's **Save**, gray until the tick changes; "
      "the bank feed's suggestion badges go or come back with it. The red x deletes it after asking."),

("h1", "5. QBO Sync"),
("h2", "Bank / Card Accounts"),
("p", "Every bank and card account in the company's QuickBooks file, with how its transactions arrive. "
      "Columns include the account name, type, Bank Feed (API, Plaid or None, with connection details), "
      "QBO Balance, Bank Balance, Last Statement Date and Last Statement Balance (credit cards connected "
      "through Plaid, for bank reconciliations; hover over a blank to see why, or over a statement to see the "
      "two before it, kept as each new one arrives), and when it last synced. Disconnecting a card clears its "
      "statement."),
("ul", [
  "**Rename, change type, retire or restore** an account from its row; these change it in QuickBooks too.",
  "**Connect** a card through Plaid from its Bank Feed column. When one bank login has several cards, a "
  "window pairs each with its account.",
  "**Reconnect** appears when a bank needs the cardholder to sign in again; it repairs the same connection "
  "and keeps its history.",
  "**Add New Account** creates an account in QuickBooks. **Sync From QBO** refreshes everything from QuickBooks now.",
  "Credit card balances show as positive amounts owed.",
  "**Card types** (All Card Types) narrows the grid to Virtuals, Non-Virtuals, API, Plaid or None, with **Select "
  "All**, **Clear** and **Close** at the bottom as on the Bank Feed's filters; the grid follows when it closes. "
  "**Connection Issues Only**, below them, keeps only the cards with a red ! (an API feed gone quiet) or a "
  "Reconnect (a bank feed that needs signing in again).",
  "A Plaid card's **CC Last 4/5** comes from the bank's own description when it names the card used "
  "(\"XXXX3004\", \"Virtual Account Number 5178\"); otherwise it is the connected account's last four.",
  "**Pages:** Bank / Card Accounts, Chart of Accounts and Vendors page like the Bank Feed -- Page size, Prev / "
  "Next, the count of results and numbered pages -- over the rows the quick search leaves.",
]),
("h2", "API Card Connections"),
("p", "One grid of every connected card program account: program, account, the card account its charges land "
      "in, whether its key works, the last pull and its result. Drag a header's edge to resize a column. A pull "
      "brings in wires and ACH on the account as well as card spend where the program reports them (Slash and "
      "Taekus); Slash's own moves between its accounts and its internal Daily Credit Card Payment sweeps stay out."),
("table", ["Control", "What it does"], [
  ["Add Connection", "In the grid's last row, at the left. Choose the program (Bill, Global Rewards, Slash or Taekus), then only what "
                     "it needs: its key (from those added on Configuration > Connectors), Slash's entity and program, "
                     "Taekus' username, and the card account its charges land in."],
  ["Upload Card List", "Above the grid. Loads the card list spreadsheet Global Rewards provides, each group of cards "
                       "into the account chosen for it, merged by GR ID."],
  ["Card Account", "On each row, a dropdown: change where that account's charges land."],
  ["Pull From / Pull Now", "Pull one account now, from the date given or from its last pull. While it pulls, the "
                           "button is a red **Stop**, which stops after the page it is on and keeps what came in. A Global Rewards account "
                           "with a long card list pulls in the background."],
  ["Stop", "Shown while a long Divvy pull runs; stops it."],
  ["Cards", "Global Rewards rows: the account's card list, edited here when short."],
  ["Account", "Taekus rows: the **Username** it signs in with, on the same line, shown by its first four "
              "characters; its red x clears it so a new one can be entered. Slash rows: **Change** chooses the entity "
              "(the company in Slash) and program."],
  ["Red x", "Disconnects the account; charges already imported stay where they are. It stays listed as Not "
            "routed while its key is on Connectors, and its red x then removes it altogether, key included."],
]),
("h2", "Chart of Accounts and Vendors"),
("p", "The company's QuickBooks accounts and vendors, mirrored. Rename, change an account's type, and retire or "
      "restore entries here. **Merge Duplicates** folds numbered duplicates such as \"Office Supplies (1)\" back into "
      "their originals when they have no posted transactions; duplicates with history must be merged in QuickBooks."),
("h2", "Deletion / Edit Scan"),
("p", "Checks that what the Engine posted is still in QuickBooks as posted. It finds transactions deleted or edited "
      "in QuickBooks afterwards, so the Engine and QuickBooks agree. It runs with the hourly refresh; **Scan Now** runs it immediately."),

("h1", "6. Broker Data and Configuration (per company)"),
("h2", "Broker Data"),
("p", "A navigation button of its own, after QBO Sync. Its tabs:"),
("table", ["Tab", "Purpose"], [
  ["Databases", "Where the broker keeps its people and its season ticket records: one row for **Profiles / "
                "Address Book** and one for **Season Ticket Records**, each listing every source it reads, its "
                "record count, last sync and a **Sync** button, which turns into a red **Stop** while the sync runs. A row's "
                "pencil changes its source (below)."],
  ["Profiles / Address Book", "How each Profiles / Address Book source reads, one row per source: its name, first "
                              "and last name and email columns. The pencil opens the source's window to change them."],
  ["Season Ticket Records", "How each Season Ticket Records source reads, one row per source: its Name, Email, "
                            "Team, League, Plan Type, Total Cost and Credit Card columns, its status layout and its "
                            "Ignore Rows Where setting. The pencil opens the source's window."],
  ["Renewal Status Words", "How this broker writes a season ticket record's renewal status (below), edited in place."],
  ["Renewal Years", "How this broker writes a season's year, per league (below), edited in place."],
  ["Teams", "Only for what the Dashboard doesn't cover for this company: teams and leagues the Dashboard doesn't "
            "have, and overrides of Dashboard teams (league, venues, bank details) set to this company's preferences, "
            "marked with a red OVERRIDE pill. Everything else comes from the Dashboard's Teams, and nothing here "
            "affects another company. A new team is typed into the grid's last row and added with its **Add** "
            "button, as on Bank Feed Rules; Add / Edit Leagues, **Download "
            "Template** and **Upload Teams From Excel** sit after the quick search; a row's Save stores it."],
  ["Payment Plans", "Likewise only for this company's own teams and overrides, never affecting another company: "
                    "the Dashboard's Payment Plans grid for the teams on this company's Teams tab -- its own teams "
                    "and its overrides of Dashboard teams (marked OVERRIDE). A plan filled in here is this company's "
                    "and replaces the Dashboard's; an override left blank uses the Dashboard's plan (marked "
                    "Dashboard plan)."],
]),
("h2", "Configuration"),
("table", ["Tab", "Purpose"], [
  ["Connectors", "The company's connections. **Add New Connector**, in the grid's last row at the left, lists, each with its icon, Airtable, Bill (formerly Divvy), Global Rewards, Google "
                 "Sheets, Microsoft OneDrive, QuickBooks, Slash, Taekus and WEX; a connector appears in the grid once "
                 "added, and the one-per-company connectors are grayed out once the company has one. The **API Keys, "
                 "IDs, etc** column holds each connector's key (Airtable's token, Google's service account JSON key, "
                 "the Microsoft App ID), each on one line with what it is, stored encrypted and shown as its last four "
                 "characters (Google's by its key id). Google Sheets reminds you to share each sheet with the Engine's address "
                 "with; OneDrive has **Sign In** until it is signed in. A card program account's description is "
                 "changed with its pencil. Record counts and last syncs are on Broker Data. The red x at the end of a row removes that "
                 "connector (after asking), and with it any Broker Data sources in that app; a row's **Save** appears beside its red x once "
                 "the row is edited. QuickBooks's red x is grayed out: the company's QuickBooks file cannot be removed."],
  ["Users", "Who can reach this company: each user, their email and their Role (Admin, Reviewer or Viewer), "
            "with the row's Save and red x beside the role."],
  ["Settings", "The company's switches (section 4). Each row has its own Save, gray until it changes."],
]),

("h2", "Broker Data sources"),
("p", "Each source is its own row on Broker Data's Databases tab: **Add Source** (after the quick search) adds one, for a "
      "broker who keeps a table per league or a file per year; a row's pencil changes it and its red x removes it. Removing a part's last source removes every record read from it as well, and nothing is read for that part until a source is added. Each source is **Airtable** (a base, table and view), **Google Sheets** or "
      "**OneDrive** (a file and tab, or **All Tabs -- One Per Season** when each season is its own tab laid out alike, "
      "where each tab's name gives its season and one **Status** column serves every tab; the header row is preselected where it was found, and can be changed). A file the list does not "
      "show, such as one in SharePoint or Teams, can be opened by pasting its link and pressing **Use Link** (OneDrive's Share \u2192 Copy "
      "link, or the address in the browser while the file is open). "
      "OneDrive's file list is re-read when it is over 10 minutes old; **Refresh List** reads it now. **Preview** reads a few real "
      "rows before anything is saved."),
("ul", [
  "**Profiles / Address Book:** a name column (or first and last name) and an email column, which may hold several "
  "addresses. A column linking to another Airtable table is read as the linked records' names.",
  "**Season Ticket Records:** Name (required -- the Name column, or both First and Last Name, so charges can be "
  "matched to the holder), Email, Team (required), League, Plan Type, Total Cost and Credit Card, and the "
  "status in one of three layouts: one column per season, one column for all seasons, or one row per season (a "
  "season column, and a status column or none, when being listed means active). **Ignore Rows Where** skips "
  "rows the source keeps that aren't season tickets, at every sync: as many conditions as needed (**Add "
  "Condition**), any one of which skips a row, each a column, an operator and a value -- for text **is**, **is "
  "not**, **is any of**, **is none of** (comma separated), **is empty**, **is not empty**; for amounts = \u2260 > < "
  "\u2265 \u2264. **Canadian Team "
  "Costs Are In** says whether Canadian teams' costs are listed in CAD (the expected amount converts them at the "
  "charge date's rate) or already in USD (no conversion).",
  "**Renewal Years** (Broker Data's own tab): for each league, how this broker writes the season "
  "beginning in 2026 -- the Engine's 26/27 -- chosen from Same, 2026, 2027, 2026/27, 2026-27, 2026/2027, "
  "2026-2027, 2025/26, 2025-26, 2025/2026 or 2025-2026. Choosing 2027 means a single year names the year the "
  "season ends; choosing a 2025 one means this broker writes two-year seasons a year behind (2025/26 is the "
  "Engine's 26/27). Every league the Dashboard lists "
  "is shown, with any this company added itself.",
  "**Renewal Status Words** (Broker Data's own tab): how you notate your season ticket renewal statuses. A "
  "grid of **Renewal Status**, **Engine Notation** (set on the Dashboard, the same for every company), **Your "
  "Notation** (this company's words, comma separated -- Ticket Guy's 'Yes' beside Renewed, say) and **Color** "
  "(the pill's color on the Bank Feed, set on the Dashboard). Three statuses are built in: **Renewed** (coded, "
  "can be added automatically), **In Progress** (coded, with the status shown) and **Not Renewed** (goes to "
  "review). A status of the company's own is added on the last row (**Add** / **Clear**) with its name, what it "
  "is treated as, its words and its color. Each row has its own Save, gray until it changes. A word can mean "
  "only one status; a status in none goes to review as unrecognized.",
  "**Cards named by email address:** when a card's name is an email address rather than a person's name, the "
  "Engine finds the season ticket records carrying that address (their Email column, or the address book's "
  "addresses for the holder), as long as the address belongs to one holder.",
  "**Teams** may be written in full, as a nickname (\"Sixers\"), a mascot or a short code (\"PHI\"). The "
  "Engine reads them through its directory of NFL, NBA, MLB, NHL, MLS, WNBA and major college teams, using the "
  "League column where a code or mascot is shared between leagues. The company's own team names win.",
  "**Seasons** follow the schedule, labeled by the year they start: 2026, 2026-2027 and 26/27 are all the 26/27 "
  "season (the 2026 MLB season, the NBA season starting in 2026). A source can read a single year as the season "
  "ending then instead.",
  "The sync reports how many rows it skipped because their season could not be read, and any teams it could not "
  "recognize. Every source is read once a day, and whenever it is saved or Sync is pressed.",
]),

("h1", "7. The Dashboard"),
("h2", "Teams & Payment Plans"),
("p", "Tabs: Teams, Payment Plans, **Renewal Status Words** (the Engine Notation and pill color of each renewal "
      "status, for every company -- Renewed, In Progress, Not Renewed, and any added on its last row, each treated "
      "as one of the three; companies add their own words on Broker Data), Marketplaces and Abbreviations."),
("p", "Opens on **Teams**. **Add / Edit Leagues** lists every league: drag one by its handle to change the order "
      "(then **Save Order**), rename it, delete one no team uses (its red x says which teams are still in a league that can't be deleted; hidden teams don't count), and choose its **Color** -- the color of its pill "
      "on every grid; the circular arrow returns it to the built-in color."),
("table", ["Tab", "What it holds"], [
  ["Teams", "Every team: its league, venues and bank details, which tell the Engine which team a bank description "
            "names. A Canada flag marks Canadian teams (never an American team whose name contains a Canadian one, such as "
            "Creighton's Bluejays); the pencil renames a team. **Add Team**, **Add / Edit "
            "Leagues**, **Download Template** and **Upload Teams From Excel** sit after the quick search (the upload "
            "reads Team, League, Venue and Bank Details from every tab; a team already listed gains the sheet's "
            "venues and bank details; nothing loads unless every row is valid) -- the Leagues window adds, renames and deletes leagues, and orders "
            "them by dragging. A bank detail or venue under 8 characters is too short to identify a team: it is "
            "saved, shown in amber, and you are told so."],
  ["Payment Plans", "Each team's renewal years, the day of the month it charges, and its schedule of monthly "
                    "percentages. A team can have more than one plan. **Update Renewal Years List** sits after the "
                    "quick search. Each row has **Save** and **Reset** (which clears the row); a team is deleted on "
                    "the Teams tab."],
  ["Marketplaces", "Marketplace names (StubHub, SeatGeek and the like) and the text in a bank detail that "
                   "identifies each, so a marketplace's charges are recognized as such."],
  ["Abbreviations", "Short forms brokers and banks use for teams and venues, and what each stands for, so "
                    "an abbreviated bank detail still names the right team."],
]),
("h3", "What the colors in the Teams grid mean"),
("p", "Each venue and bank detail in the Teams grid is a claim on bank descriptions. Its look says whether it is used:"),
("pills", [
  ("plain text", "#ffffff", "#1f1f1d", "#d7dde5", "Active: used to identify this team."),
  ("shared", "#fee2e2", "#991b1b", "#fca5a5", "Claims nothing -- another team claims the same text just as strongly, so neither is chosen."),
  ("too short", "#fef3c7", "#92400e", "#fcd34d", "Too short to identify a team safely (under 8 characters). Saved, but not used; use a longer form."),
  ("outranked", "#f1f5f9", "#64748b", "#cbd5e1", "Not used here -- a bank-detail note claims this text, and a note beats a venue."),
  ("off", "#f1f5f9", "#94a3b8", "#cbd5e1", "This team is switched off for matching."),
]),

("h2", "Algorithms"),
("table", ["Tab", "What it holds"], [
  ["Signals", "How much each piece of evidence counts toward a match: amount, date, names, team and more."],
  ["Gates", "The date and amount limits a match must pass before it is scored at all. Each row has its own Save, "
            "shown once the row is changed."],
  ["Strengths", "The bands: their names, where each starts and their colors. A new band is filled in on the grid's last "
                "row and added with **Add**; each row has its own Save; the red x removes "
                "one. Charges are regraded at their next rescoring."],
  ["Monthly Payments", "How installment plans are recognized and checked. Each row has its own Save."],
  ["Ignored Words", "Words in bank descriptions that should not count as evidence."],
  ["Exceptions", "Deliberate exceptions to the usual matching: when a **Bank Detail** contains or equals a value, "
                 "match it to a chosen vendor; when the **Vendor** on the bill contains or equals a value, treat the "
                 "buyer email as a perfect match (for a seller whose bills carry its own address, not the buyer's)."],
  ["Suggestions", "Each setting with what it means beside it, as on Signals, and its own Save. What makes the Engine suggest a vendor and category from past coding, for every company: how "
                  "many **similar past codings** it needs (5 to start), how many must **agree** (90% to start), what "
                  "makes two charges **similar** (the bank detail always; the card account, the cardholder and the "
                  "amount can be added), and whether it learns **only from charges a person coded**. A suggestion is "
                  "marked 'from history (12 of 13)' and is never posted on its own."],
]),
("h2", "Tags"),
("table", ["Tab", "Purpose"], [
  ["Canada", "Canadian venues and teams, and the CAD to USD rate. The rate updates daily from the Bank of Canada "
             "(tick box) or is set by hand, which turns the daily update off. **Rate History** shows the last 90 days' rates; **Load Earlier Rates** reaches back a year at a time (to 2017), **Show Fewer** returns to the last 90 days, and **Go to date** finds a day."],
  ["CC Payments / Wires", "Patterns that identify card payments and wires."],
  ["CC Vendors", "The vendor a card's payments use, by the card account's name: Cap One pays Capital One, BOA pays "
                 "Bank of America. Whole words; the longest match wins."],
  ["Virtual Cards", "Patterns that identify virtual cards."],
]),
("h2", "Companies and User Management"),
("p", "Companies lists each company and its QuickBooks connection. User Management has three tabs:"),
("table", ["Tab", "What it holds"], [
  ["Users", "Every account: its email, name, **Role**, companies and last sign-in. Change a role in its column "
            "(the row's Save turns green); an owner stays an owner. Deactivate stops a person signing in."],
  ["Settings", "**Minutes of inactivity before automatic sign out**, for everyone (5 to 720)."],
  ["Roles", "The built-in roles (Owner, Admin, Reviewer, Viewer) and roles you add. An added role starts from an "
            "access level (Admin, Reviewer or Viewer); untick the abilities it may not have: Add & Match, Edit "
            "Transactions, Delete Permanently, Upload CSVs, Edit Bills, Bank Feed Rules, Profiles & Suggestions, "
            "Configuration, Dashboard Settings, Export to Excel. It can never do more than its level allows."],
]),

("h1", "8. The Audit Log"),
("p", "Every import, decision, posting, setting change and connection change, with who did it and when. Filter by "
      "action, person, date or text, and export to Excel. Inside a company it shows that company; on the Dashboard it "
      "shows everything; in Multi-Company it shows every company you can access."),

("h1", "9. Multi-Company"),
("p", "Choose Multi-Company in the picker (between the two divider lines) to work across companies in one "
      "view. Each grid gains a Company "
      "column (after the Action column where there is one), and a **Companies** picker narrows the view; the "
      "choice carries across screens."),
("ul", [
  "**Every action works within the row's own company:** a charge is matched only to its own company's bills and "
  "posted to its own company's QuickBooks file.",
  "**Shared Bank Feed Rules:** a rule added here is one shared rule for the companies you tick, edited once for "
  "all of them. Its Company cell (**Shared \u00b7 3 Companies**) opens the list of companies using it; unticking a "
  "company makes it ignore the rule; the row's Save turns green and applies the change. Multi-Company shows only "
  "shared rules. Inside a company, only the shared rules it uses are listed, each with a **Shared** tag beside "
  "its name and an **Ignore (Shared Rules)** tick -- ticking it takes the rule off that company (it is then used "
  "again from Multi-Company). A shared rule can't be removed inside a company -- only in Multi-Company. A company's own rules are tried before shared ones, and a shared rule does not apply where the "
  "company's QuickBooks file lacks its vendor or category.",
  "**Undo** reverses your latest action in whichever company it was taken.",
  "**Configuration** is not available in this mode. Merging duplicate accounts needs a single company.",
]),

("h1", "10. Security"),
("ul", [
  "API keys are encrypted in the database and never sent to a browser; screens only show them masked.",
  "Sign-in requires the two-step code.",
  "**Profile**, in the menu behind your initials, changes your name and your password (which needs your current one).",
  "Keep access to the hosting account and the Intuit developer account limited and protected with "
  "two-factor sign-in: together they hold everything needed to reach the data.",
  "If a key may have been exposed, generate a new one with the provider and paste it on Connectors.",
]),

("h1", "11. Troubleshooting"),
("table", ["Problem", "What to do"], [
  ["\"STH is not synced yet\" banner", "Shown on one company's Bank Feed when its Season Ticket Records are set up on "
   "Broker Data but none have been read yet. It goes on the next Bank Feed load after the first sync finishes "
   "(Sync on Broker Data starts one). It never shows in Multi-Company, on the Dashboard or for a company with no "
   "Season Ticket Records source."],
  ["An upload is refused", "The message says what the file needs. A CSV must have a date column and an amount "
                           "(or debit and credit) column."],
  ["A card stopped syncing", "Look for Reconnect in its Bank Feed column on Bank / Card Accounts."],
  ["QuickBooks refused a posting", "The row stays in For Review with QuickBooks' reason on it. Fix the cause, then post again."],
  ["Transactions appear under the wrong account", "Check the account's name and Type on Bank / Card Accounts."],
  ["A charge was posted by mistake", "Use Undo straight away, or Return to For Review from Categorized."],
  ["A match is missing", "Check Open Bills for the bill and the Gates in Algorithms; Force Match can pair them by hand."],
]),
]
