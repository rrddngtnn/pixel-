package com.pixel.oauthhelper;

import android.Manifest;
import android.accounts.Account;
import android.accounts.AccountManager;
import android.accounts.AccountManagerCallback;
import android.accounts.AccountManagerFuture;
import android.accounts.AuthenticatorException;
import android.accounts.OperationCanceledException;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final int REQ_GET_ACCOUNTS = 10;
    private static final int REQ_CHOOSE_ACCOUNT = 11;
    private static final int REQ_AUTH = 12;
    private static final String DEFAULT_SCOPE = "oauth2:https://www.google.com/accounts/OAuthLogin";

    private AccountManager accountManager;
    private TextView statusView;
    private String requestedEmail;
    private String scope;
    private Account selectedAccount;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        statusView = new TextView(this);
        statusView.setTextSize(16);
        statusView.setPadding(32, 40, 32, 32);
        setContentView(statusView);

        accountManager = AccountManager.get(this);
        requestedEmail = trim(getIntent().getStringExtra("email"));
        scope = trim(getIntent().getStringExtra("scope"));
        if (scope == null) {
            scope = DEFAULT_SCOPE;
        }

        setStatus("Preparing Google account token...");
        startFlow();
    }

    private void startFlow() {
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.GET_ACCOUNTS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.GET_ACCOUNTS}, REQ_GET_ACCOUNTS);
            return;
        }
        chooseAccount();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_GET_ACCOUNTS) {
            chooseAccount();
        }
    }

    private void chooseAccount() {
        Account[] accounts = accountManager.getAccountsByType("com.google");
        if (requestedEmail != null) {
            for (Account account : accounts) {
                if (requestedEmail.equalsIgnoreCase(account.name)) {
                    requestToken(account);
                    return;
                }
            }
        }

        if (requestedEmail == null && accounts.length == 1) {
            requestToken(accounts[0]);
            return;
        }

        setStatus("Choose the Google account to use for the offer.");
        Intent intent = AccountManager.newChooseAccountIntent(
                null,
                null,
                new String[]{"com.google"},
                true,
                "Choose the Google account for Google One offer claiming",
                null,
                null,
                null
        );
        startActivityForResult(intent, REQ_CHOOSE_ACCOUNT);
    }

    private void requestToken(Account account) {
        selectedAccount = account;
        setStatus("Requesting OAuthLogin token for " + account.name + "...");
        Bundle options = new Bundle();
        accountManager.getAuthToken(account, scope, options, this, new AccountManagerCallback<Bundle>() {
            @Override
            public void run(AccountManagerFuture<Bundle> future) {
                try {
                    Bundle result = future.getResult();
                    Intent intent = result.getParcelable(AccountManager.KEY_INTENT);
                    if (intent != null) {
                        setStatus("Waiting for Google authorization...");
                        startActivityForResult(intent, REQ_AUTH);
                        return;
                    }

                    String token = result.getString(AccountManager.KEY_AUTHTOKEN);
                    String email = result.getString(AccountManager.KEY_ACCOUNT_NAME);
                    if (email == null) {
                        email = selectedAccount.name;
                    }
                    if (token == null || token.length() == 0) {
                        saveError("Google returned an empty token.");
                        return;
                    }
                    saveOk(email, token);
                } catch (OperationCanceledException e) {
                    saveError("Authorization was cancelled.");
                } catch (AuthenticatorException e) {
                    saveError("Authenticator error: " + e.getMessage());
                } catch (IOException e) {
                    saveError("I/O error: " + e.getMessage());
                } catch (Exception e) {
                    saveError("Unexpected error: " + e.getClass().getSimpleName() + ": " + e.getMessage());
                }
            }
        }, null);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_CHOOSE_ACCOUNT) {
            if (resultCode != RESULT_OK || data == null) {
                saveError("No Google account was selected.");
                return;
            }
            String name = data.getStringExtra(AccountManager.KEY_ACCOUNT_NAME);
            String type = data.getStringExtra(AccountManager.KEY_ACCOUNT_TYPE);
            if (name == null || type == null) {
                saveError("Account picker did not return an account.");
                return;
            }
            requestToken(new Account(name, type));
            return;
        }

        if (requestCode == REQ_AUTH) {
            if (resultCode != RESULT_OK) {
                saveError("Google authorization was not approved.");
                return;
            }
            if (selectedAccount == null) {
                saveError("Authorization returned without a selected account.");
                return;
            }
            requestToken(selectedAccount);
        }
    }

    private void saveOk(String email, String token) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("ok", true);
            obj.put("email", email);
            obj.put("scope", scope);
            obj.put("oauth_login_token", token);
            obj.put("ts", System.currentTimeMillis() / 1000L);
            writeResult(obj);
            setStatus("Token captured for " + email + ". You can return to the computer.");
        } catch (Exception e) {
            setStatus("Failed to write result: " + e.getMessage());
        }
        finishSoon();
    }

    private void saveError(String message) {
        try {
            JSONObject obj = new JSONObject();
            obj.put("ok", false);
            obj.put("error", message == null ? "Unknown error" : message);
            obj.put("scope", scope);
            writeResult(obj);
        } catch (Exception ignored) {
        }
        setStatus(message == null ? "Failed." : message);
        finishSoon();
    }

    private void writeResult(JSONObject obj) throws IOException {
        File out = new File(getFilesDir(), "token.json");
        FileOutputStream fos = new FileOutputStream(out, false);
        try {
            fos.write(obj.toString().getBytes(StandardCharsets.UTF_8));
            fos.flush();
        } finally {
            fos.close();
        }
    }

    private void finishSoon() {
        statusView.postDelayed(new Runnable() {
            @Override
            public void run() {
                finish();
            }
        }, 1200);
    }

    private void setStatus(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                statusView.setText(text);
            }
        });
    }

    private static String trim(String value) {
        if (value == null) {
            return null;
        }
        String v = value.trim();
        return v.length() == 0 ? null : v;
    }
}
