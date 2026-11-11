using Microsoft.UI.Xaml;

namespace UniversalClipboard;

public partial class App : Application
{
    private Window? _window;

    public App()
    {
        InitializeComponent();
        UnhandledException += OnUnhandledException;
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
            WriteCrashLog(ex.ToString());
            throw;
        }
    }

    private static void OnUnhandledException(object sender, Microsoft.UI.Xaml.UnhandledExceptionEventArgs e)
    {
        WriteCrashLog($"{e.Message}\n{e.Exception}");
    }

    private static void WriteCrashLog(string text)
    {
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "UniversalClipboard");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "ui-crash.log"), $"[{DateTime.Now:O}]\n{text}\n\n");
        }
        catch
        {
            // Never throw from the crash logger.
        }
    }
}
