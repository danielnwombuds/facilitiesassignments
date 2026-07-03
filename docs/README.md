# WA DSHS Facilities Map (GitHub Pages)

This folder is the GitHub Pages site. It reads `data/facilities.geojson` and plots every facility.

- **Pin color** = assignee (from the spreadsheet `color` column)
- **Pin shape** = facility subtype
- **Click a pin** = details pane on the left

## Enable GitHub Pages

1. Push this repo to GitHub.
2. Go to **Settings → Pages**.
3. Under **Build and deployment**, set:
   - Source: **Deploy from a branch**
   - Branch: `main` (or your default branch)
   - Folder: `/docs`
4. Save. The site will be at:

   `https://<your-user-or-org>.github.io/<repo-name>/`
