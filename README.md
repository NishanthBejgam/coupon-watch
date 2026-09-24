# coupon-watch

Watches for Amazon.in's **jewellery cashback reward** and publishes one file
that says whether it is live.

The "jewellery coupon" is an Amazon Pay *reward* — a collect card at
`amazon.in/h/rewards/dp/amzn1.rewards.rewardAd.<ID>`. Its page says, logged
out, whether it can be collected (`rewardStatus`) and what it pays ("GET FLAT
₹1500 BACK · Min order: ₹15000 · Valid till 11 Nov"). Every half hour, and
straight away whenever Reward Switch spots a new reward, this repo:

1. **takes** new reward IDs from [Reward Switch](https://github.com/NishanthBejgam/rewardswitch)'s
   15-minute watcher, which reads DesiDime and public Telegram previews and
   commits every id it finds to `seed/catalog.json` — a hint, never the answer;
2. **confirms** every watched ID (a stable `jewellery` vanity slug plus every ID
   ever seen) against Amazon's own page;
3. writes **[`signal/coupon.json`](signal/coupon.json)** — `"status": "live"`
   with the terms, or `"status": "none"`.

[AmazonGold](https://amazongold.yourcardjourney.store) reads that file at build
time and locks its Gold-coupon control onto it; a change also dispatches a
rebuild. Bookkeeping (IDs, cursors, last read) lives in `signal/rewards.json`.

```
python watch_coupon.py              one tick, write signal/
python watch_coupon.py --no-harvest Amazon only
```

Public so that the half-hourly GitHub Actions run is free. Nothing here is
secret: the script, the IDs, and a signal the deal channels post anyway.
