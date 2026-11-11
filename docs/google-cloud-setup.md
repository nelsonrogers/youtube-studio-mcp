# Google Cloud setup (one time, ~10 minutes)

YouTube only lets apps read Studio analytics through OAuth, so you need your own (free) Google
Cloud OAuth client. Nothing is shared with the authors of this project: the client, your tokens and
your data stay on your computer and in your own Google Cloud project.

> You can create the Google Cloud project with **any** Google account (for example a partner's or
> a manager's) and then sign in to YouTube with the account that owns the channel. That account
> just needs to be able to click through the consent screen.

## 1. Create a project

1. Open <https://console.cloud.google.com/projectcreate>.
2. Name it e.g. `YouTube Studio MCP` and click **Create**. Make sure it is selected in the top bar.

## 2. Enable the YouTube APIs

Open each link (with your project selected) and click **Enable**:

- [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com): channel, videos, playlists, comments
- [YouTube Analytics API](https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com): all Studio analytics
- [YouTube Reporting API](https://console.cloud.google.com/apis/library/youtubereporting.googleapis.com): impressions & click-through rate (optional, recommended)

## 3. Configure the consent screen

Go to **APIs & Services → OAuth consent screen** (shown as **Google Auth Platform** in the new
console) and click **Get started**:

1. **App information:** any name (e.g. `YouTube Studio MCP`) and your email as the support email.
2. **Audience:** choose **External** (or **Internal** if the channel belongs to a Google Workspace
   organisation you administer).
3. **Contact information:** your email. Accept the policy and click **Create**.
4. *(Recommended)* **Data access → Add or remove scopes**, then add:
   - `.../auth/youtube.readonly`
   - `.../auth/yt-analytics.readonly`
   - `.../auth/yt-analytics-monetary.readonly` (only needed for revenue)
   - `.../auth/youtube.force-ssl` (only if you will enable the optional write tools)

### Avoid weekly sign-ins

While an External app is in **Testing**, Google expires its sign-ins after **7 days**, so you would
have to run `youtube-studio-mcp auth` every week. To avoid that:

1. Open **Audience** and click **Publish app** so the status becomes **In production**.
2. You do **not** need to submit the app for verification for personal use. Unverified apps can be
   used by up to 100 Google accounts. Each person sees a "Google hasn't verified this app" warning
   when signing in (see step 5).

If you prefer to stay in Testing, add the channel owner's Google account under
**Audience → Test users** and expect to sign in again weekly.

## 4. Create the OAuth client

1. Go to **APIs & Services → Credentials** (or **Google Auth Platform → Clients**).
2. **Create credentials → OAuth client ID**.
3. Application type: **Desktop app**. Name: anything. Click **Create**.
4. Click **Download JSON**. You get a file like `client_secret_1234-abc.apps.googleusercontent.com.json`.

Keep this file private. It identifies *your* app (it can't access any data on its own).

## 5. Sign in

```bash
youtube-studio-mcp auth --client-secret ~/Downloads/client_secret_XXXX.json
```

The command copies the client file into the config folder (`~/.config/youtube-studio-mcp/` or
`%APPDATA%\youtube-studio-mcp\`) and opens your browser:

1. Choose the Google account that owns the channel. **If the channel is a Brand Account**, Google
   shows a second screen listing your channels: pick the channel.
2. "Google hasn't verified this app": click **Advanced → Go to YouTube Studio MCP (unsafe)**. It's
   your own app; the warning only appears because it hasn't been through Google's review.
3. Tick **all** the permission checkboxes and click **Continue**.

The terminal then prints `Signed in. Channel: …`. Next, add the server to your AI app (see the
[README](../README.md#2-connect-your-ai-app)).

### Notes

- **Channel managers:** the YouTube Analytics API only serves data for the channel the signed-in
  account (or Brand Account) *owns*. Being a manager in Studio's permissions isn't enough. Sign in
  as the channel itself.
- **Several channels:** use profiles, e.g. `youtube-studio-mcp auth --profile gaming`, then set
  `YOUTUBE_STUDIO_MCP_PROFILE=gaming` for that server entry.
- **Revoking access:** `youtube-studio-mcp logout`, or remove the app at
  <https://myaccount.google.com/permissions>.
- **Quota:** the default YouTube Data API quota is 10,000 units/day; this server uses about 1 unit
  per 50 videos listed, so normal use never comes close. Analytics queries have their own, generous
  quota.

## Troubleshooting

| Message | Fix |
|---|---|
| `... API has not been used in project ... or it is disabled` | Enable that API (step 2). The error includes a direct link. Wait a minute and retry. |
| `access_denied` / "Access blocked: app has not completed the Google verification process" | The app is in Testing and the account isn't a test user: publish the app (step 3) or add the account as a test user. |
| `Google rejected the stored sign-in` every ~7 days | The app is still in Testing: publish it (step 3), then run `auth` again. |
| `missing a permission` | Run `auth` again and tick every checkbox on the consent screen. |
| `This Google account has no YouTube channel` | Sign in again and pick the Brand Account/channel on the second screen. |
| `is a 'Web application' OAuth client` | Create a **Desktop app** client (step 4). |
