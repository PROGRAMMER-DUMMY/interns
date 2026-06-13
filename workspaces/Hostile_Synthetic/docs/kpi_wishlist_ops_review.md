# Notes from the quarterly ops review (typed up by Sandra, pls excuse typos)

Marco (Ops Director, Brindle & Vance Logistics) talked through the numbers he wants on the new
board pack. I've written them down as close to how he said them as I could. He was very clear
he does not want "another spreadsheet of caveats", he wants the numbers.

Some of this references the old TMS screens, some of it the finance system. IT say it's all in
the warehouse extract now.

### KPI 1 - On-time delivery rate

Marco: "Simple one. Of everything we delivered, what share got there on time. On time means
within the service promise the customer bought - express is next day, and so on down the tiers.
Monthly, and split it by carrier so I can beat up the bad ones."

(Sandra's note: not sure if "on time" is counted from when the job was booked or from when we
actually picked it up. Marco waved his hand at this question.)

### KPI 2 - Monthly revenue per active account

"Take what we actually billed - not the quoted numbers the sales lads put in, billed - and give
me revenue per active account, by month. Active means they're trading with us, you know what I
mean."

(He did not define trading-with-us. Finance say an account is active if it has an invoice in the
period; CRM say it's the account status flag. These disagree.)

### KPI 3 - Average depot dwell hours

"When a trailer hits one of our depots, how long does the freight sit before it moves out again.
Average hours, by depot. The Carlisle lot swear it's under four hours, I don't believe them."

### KPI 4 - Damage claim rate by carrier

"Out of the jobs each carrier delivered, how many ended up with a damage claim against them.
Percentage, by carrier. Cedar Express feel high to me."

(Marco said "damage" - unclear if he'd count loss and delay claims too, that's most of what
the claims desk logs.)

### KPI 5 - Share of shipments upgraded to premium after dispatch

"The CS team keep upgrading jobs to premium service after they've already left the dock to make
the SLA look better. I want the percentage of shipments that got upgraded to a premium service
after dispatch, by month, so I can stamp it out."

(Sandra's note: I asked the TMS admin and he'd never heard of post-dispatch upgrades, but Marco
was adamant this happens.)

### KPI 6 - Fleet utilization

"What share of our own fleet is actually working. Utilization, weekly. You know - of the
vehicles we own, how many are out doing legs versus sat in the yard or in the workshop."

(Does "fleet" include disposed vehicles? Vehicles off-road (VOR)? He didn't say.)

### KPI 7 - Invoice dispute rate

"How much of our billing ends up disputed. Percentage of invoices that get a dispute raised,
monthly. Finance will tell you it's tiny, the CS inbox says otherwise."

(Per-invoice or per-invoice-line? The billing extract is line level, disputes are raised against
the invoice number.)

### KPI 8 - Average cost per kilogram by lane

"Take the freight charge and divide by the weight, lane by lane, so I can see which corridors
are bleeding us. Watch the weights - half the legacy depots still key imperial."

### KPI 9 - Quarterly account churn

"How many accounts we lose a quarter. Churn. Every logistics business counts this differently,
pick something sensible and tell me what you picked."

### KPI 10 - Perfect shipment rate

"The holy grail one: delivered on time, no damage claim, no billing dispute. What share of jobs
clears all three. That's the number for the board."

---

Sandra: I also attached the data dictionary the BI contractor left us in January. He left in a
hurry so I can't promise it's all still right.
