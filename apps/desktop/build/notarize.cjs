const path = require("node:path");
const { notarize } = require("@electron/notarize");

exports.default = async function notarizeMac(context) {
  if (context.electronPlatformName !== "darwin") return;
  const appPath = path.join(
    context.appOutDir,
    `${context.packager.appInfo.productFilename}.app`
  );
  const apiKey = process.env.APPLE_API_KEY;
  const apiKeyId = process.env.APPLE_API_KEY_ID;
  const apiIssuer = process.env.APPLE_API_ISSUER;
  const appleId = process.env.APPLE_ID;
  const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  if (apiKey && apiKeyId && apiIssuer) {
    await notarize({ appPath, appleApiKey: apiKey, appleApiKeyId: apiKeyId, appleApiIssuer: apiIssuer });
    return;
  }
  if (appleId && appleIdPassword && teamId) {
    await notarize({ appPath, appleId, appleIdPassword, teamId });
    return;
  }
  if (process.env.ZAGENT_REQUIRE_NOTARIZATION === "1") {
    throw new Error("Apple notarization credentials are required but not configured");
  }
  console.warn("Apple notarization skipped: credentials are not configured");
};
