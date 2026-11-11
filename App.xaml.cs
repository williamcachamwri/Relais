using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace UniversalClipboard;

public partial class App : Application
{
    private Window? _window;

    public App()
    {
        InitializeComponent();

        // 1. WinUI 3 UI Thread Unhandled Exception Handler
        UnhandledException += OnUnhandledException;

        // 2. Global AppDomain Unhandled Exception Handler
        AppDomain.CurrentDomain.UnhandledException += OnAppDomainUnhandledException;

        // 3. Unobserved Background Task Exception Handler
        TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            _window = new MainWindow();
            _window.Activate();
        }
        catch (Exception ex)
        {
            WriteCrashLog($"Startup failed in OnLaunched:\n{ex}");
            throw;
        }
    }

    private void OnUnhandledException(object sender, Microsoft.UI.Xaml.UnhandledExceptionEventArgs e)
    {
        WriteCrashLog($"[WinUI UnhandledException] {e.Message}\n{e.Exception}");
        e.Handled = true; // Prevent app process from crashing immediately

        if (_window is MainWindow mw)
        {
            try
            {
                mw.DispatcherQueue.TryEnqueue(() =>
                {
                    mw.ShowNotification(
                        !string.IsNullOrWhiteSpace(e.Message) ? e.Message : "An unexpected UI error occurred.",
                        InfoBarSeverity.Error,
                        "Application Warning"
                    );
                });
            }
            catch
            {
                // Ignore fallback failure
            }
        }
    }

    private static void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
    {
        WriteCrashLog($"[UnobservedTaskException] {e.Exception.Message}\n{e.Exception}");
        e.SetObserved(); // Mark exception as observed to prevent process termination
    }

    private static void OnAppDomainUnhandledException(object sender, System.UnhandledExceptionEventArgs e)
    {
        var exStr = e.ExceptionObject is Exception ex ? ex.ToString() : e.ExceptionObject?.ToString() ?? "Unknown";
        WriteCrashLog($"[AppDomain UnhandledException] IsTerminating={e.IsTerminating}\n{exStr}");
    }

    private static void WriteCrashLog(string text)
    {
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "UniversalClipboard");
            Directory.CreateDirectory(dir);
            var logPath = Path.Combine(dir, "ui-crash.log");

            // Rotate if log exceeds 1MB
            if (File.Exists(logPath) && new FileInfo(logPath).Length > 1024 * 1024)
            {
                var bakPath = Path.Combine(dir, "ui-crash.old.log");
                File.Move(logPath, bakPath, true);
            }

            File.AppendAllText(logPath, $"[{DateTime.Now:O}]\n{text}\n\n");
        }
        catch
        {
            // Never throw from the crash logger
        }
    }
}
