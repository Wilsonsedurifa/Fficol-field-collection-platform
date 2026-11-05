# Collector Monitoring verification

Automated command:

```powershell
.\env\Scripts\python.exe manage.py test apps.user.monitoring.tests --keepdb
```

The nine database tests cover authorization, revoked client scope, exclusion of
Back Office identities, missing activity, first-action deadlines, requested versus
authorized follow-ups, deduplication of overdue assignments, isolation of another
client's payment activity, filtered summaries, pagination and empty states.
Fixtures are created only in Django's test database.

## Browser acceptance checklist

Actual browser checks remain pending. Use fictional records in an isolated test
environment, not production customer data.

- Open `/back-office/collectors/` at 1600px, 1024px, 768px and 375px widths.
- Verify summary grids show six, three and two columns at the relevant breakpoints.
- Check filters and buttons align, and the checkbox label remains beside its box.
- Check the page does not scroll horizontally; the table may scroll in its container.
- Open View details and check long account references do not widen the page.
- Apply search, client, availability, approval and overdue filters; verify selected values remain visible.
- With more than 20 fictional matching collectors, go to Next and Previous;
  selected filters and summary counts should persist.
- Check singular, plural and empty results; pagination is hidden for one page.
- Confirm Online is displayed as declared availability, not live presence.
- Check a collector with overdue first action and overdue authorized follow-up
  contributes one overdue assignment, not two.

Monitoring does not send deadline reminders or automatically reassign accounts.
Those require a separately configured scheduled worker and approved reminder timing.
