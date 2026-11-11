using System.Numerics;
using Microsoft.UI;
using Microsoft.UI.Composition;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Hosting;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using UniversalClipboard.Services;
using Windows.Graphics;
using Windows.UI;

namespace UniversalClipboard;

public sealed partial class MainWindow : Window
{
    private readonly ClipboardBackendService _backend = new();
    private readonly DispatcherTimer _pollTimer;

    private List<ClipboardHistoryItem> _allItems = [];
    private int _lastHistoryRevision = -1;
    private string _activeFilter = "all";
    private string _searchQuery = "";
    private bool _hasAutoSelectedInitialView;

    // Apple-Design System Palette (Dark Mode)
    private static readonly SolidColorBrush BrushAppleBlue = new(ColorHelper.FromArgb(0xFF, 0x0A, 0x84, 0xFF));
    private static readonly SolidColorBrush BrushAppleGreen = new(ColorHelper.FromArgb(0xFF, 0x30, 0xD1, 0x58));
    private static readonly SolidColorBrush BrushAppleAmber = new(ColorHelper.FromArgb(0xFF, 0xFF, 0x9F, 0x0A));
    private static readonly SolidColorBrush BrushApplePurple = new(ColorHelper.FromArgb(0xFF, 0xBF, 0x5A, 0xF2));
    private static readonly SolidColorBrush BrushAppleGold = new(ColorHelper.FromArgb(0xFF, 0xFF, 0xD6, 0x0A));
    private static readonly SolidColorBrush BrushAppleRed = new(ColorHelper.FromArgb(0xFF, 0xFF, 0x45, 0x3A));

    private static readonly SolidColorBrush BrushGreenTint = new(ColorHelper.FromArgb(0x22, 0x30, 0xD1, 0x58));
    private static readonly SolidColorBrush BrushGreenBorder = new(ColorHelper.FromArgb(0x55, 0x30, 0xD1, 0x58));
    private static readonly SolidColorBrush BrushBlueTint = new(ColorHelper.FromArgb(0x22, 0x0A, 0x84, 0xFF));
    private static readonly SolidColorBrush BrushBlueBorder = new(ColorHelper.FromArgb(0x55, 0x0A, 0x84, 0xFF));
    private static readonly SolidColorBrush BrushAmberTint = new(ColorHelper.FromArgb(0x22, 0xFF, 0x9F, 0x0A));
    private static readonly SolidColorBrush BrushAmberBorder = new(ColorHelper.FromArgb(0x55, 0xFF, 0x9F, 0x0A));
    private static readonly SolidColorBrush BrushPurpleTint = new(ColorHelper.FromArgb(0x22, 0xBF, 0x5A, 0xF2));

    private static readonly SolidColorBrush BrushTextPrimary = new(ColorHelper.FromArgb(0xFF, 0xF5, 0xF5, 0xF7));
    private static readonly SolidColorBrush BrushTextSecondary = new(ColorHelper.FromArgb(0xFF, 0x98, 0x98, 0x9F));
    private static readonly SolidColorBrush BrushTextMuted = new(ColorHelper.FromArgb(0xFF, 0x6E, 0x6E, 0x77));

    private static readonly SolidColorBrush BrushCardSurface = new(ColorHelper.FromArgb(0xFF, 0x15, 0x18, 0x21));
    private static readonly SolidColorBrush BrushCardHover = new(ColorHelper.FromArgb(0xFF, 0x1B, 0x1F, 0x2B));
    private static readonly SolidColorBrush BrushCardBorder = new(ColorHelper.FromArgb(0x1E, 0xFF, 0xFF, 0xFF));
    private static readonly SolidColorBrush BrushPillInactive = new(ColorHelper.FromArgb(0xFF, 0x17, 0x1A, 0x24));
    private static readonly SolidColorBrush BrushNavActive = new(ColorHelper.FromArgb(0xFF, 0x2C, 0x31, 0x40));
    private static readonly SolidColorBrush BrushTransparent = new(Colors.Transparent);

    public MainWindow()
    {
        InitializeComponent();

        ConfigureWindowChrome();
        AttachInteractivePressStates();

        _pollTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(750),
        };
        _pollTimer.Tick += OnPollTimerTick;

        Closed += OnWindowClosed;
        _ = InitializeBackendAsync();
    }

    private void ConfigureWindowChrome()
    {
        try
        {
            SystemBackdrop = new MicaBackdrop();
        }
        catch
        {
            // Fallback to solid dark surface
        }

        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleBarDragRegion);

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        appWindow.Resize(new SizeInt32(980, 740));

        if (appWindow.TitleBar is { } titleBar)
        {
            titleBar.ButtonBackgroundColor = Colors.Transparent;
            titleBar.ButtonInactiveBackgroundColor = Colors.Transparent;
            titleBar.ButtonForegroundColor = ColorHelper.FromArgb(0xFF, 0xF5, 0xF5, 0xF7);
            titleBar.ButtonInactiveForegroundColor = ColorHelper.FromArgb(0xFF, 0x6E, 0x6E, 0x77);
            titleBar.ButtonHoverBackgroundColor = ColorHelper.FromArgb(0x20, 0xFF, 0xFF, 0xFF);
            titleBar.ButtonHoverForegroundColor = Colors.White;
        }
    }

    private void AttachInteractivePressStates()
    {
        AttachPressFeedback(NavClipboardBtn, 0.96f);
        AttachPressFeedback(NavOnboardingBtn, 0.96f);
        AttachPressFeedback(SyncNowBtn, 0.96f);
        AttachPressFeedback(FinishOnboardingBtn, 0.97f);
        AttachPressFeedback(PairWifiBtn, 0.97f);
        AttachPressFeedback(ClearHistoryBtn, 0.96f);
        AttachPressFeedback(FilterAllBtn, 0.96f);
        AttachPressFeedback(FilterTextBtn, 0.96f);
        AttachPressFeedback(FilterUrlBtn, 0.96f);
        AttachPressFeedback(FilterImageBtn, 0.96f);
        AttachPressFeedback(FilterPinnedBtn, 0.96f);
    }

    private static void AttachPressFeedback(UIElement element, float pressedScale = 0.98f)
    {
        element.PointerPressed += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateElementScale(fe, pressedScale, 105);
            }
        };
        element.PointerReleased += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateElementScale(fe, 1.0f, 155);
            }
        };
        element.PointerExited += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateElementScale(fe, 1.0f, 155);
            }
        };
    }

    private static void AnimateElementScale(FrameworkElement element, float targetScale, int durationMs)
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(element);
            var width = (float)element.ActualWidth;
            var height = (float)element.ActualHeight;
            if (width > 0 && height > 0)
            {
                visual.CenterPoint = new Vector3(width / 2f, height / 2f, 0f);
            }

            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.23f, 1.0f), new Vector2(0.32f, 1.0f));
            var anim = compositor.CreateVector3KeyFrameAnimation();
            anim.InsertKeyFrame(1.0f, new Vector3(targetScale, targetScale, 1.0f), easing);
            anim.Duration = TimeSpan.FromMilliseconds(durationMs);
            visual.StartAnimation("Scale", anim);
        }
        catch
        {
            // Ignore composition error if visual tree not ready
        }
    }

    private static void AnimateViewTransition(FrameworkElement targetView)
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(targetView);
            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.23f, 1.0f), new Vector2(0.32f, 1.0f));

            var fadeAnim = compositor.CreateScalarKeyFrameAnimation();
            fadeAnim.InsertKeyFrame(0.0f, 0.0f);
            fadeAnim.InsertKeyFrame(1.0f, 1.0f, easing);
            fadeAnim.Duration = TimeSpan.FromMilliseconds(180);

            var offsetAnim = compositor.CreateVector3KeyFrameAnimation();
            offsetAnim.InsertKeyFrame(0.0f, new Vector3(0f, 8f, 0f));
            offsetAnim.InsertKeyFrame(1.0f, Vector3.Zero, easing);
            offsetAnim.Duration = TimeSpan.FromMilliseconds(180);

            visual.StartAnimation("Opacity", fadeAnim);
            visual.StartAnimation("Offset", offsetAnim);
        }
        catch
        {
            // Ignore composition error
        }
    }

    private async Task InitializeBackendAsync()
    {
        await _backend.EnsureBackendRunningAsync();
        await RefreshStatusAndHistoryAsync();
        _pollTimer.Start();
    }

    private async void OnPollTimerTick(object? sender, object e)
    {
        await RefreshStatusAndHistoryAsync();
    }

    private async Task RefreshStatusAndHistoryAsync()
    {
        var status = await _backend.GetStatusAsync();
        if (status is null)
        {
            HeaderStatusDot.Fill = BrushAppleAmber;
            HeaderDeviceText.Text = "Starting CoreDevice service...";
            return;
        }

        UpdateStatusUi(status);

        if (!_hasAutoSelectedInitialView)
        {
            _hasAutoSelectedInitialView = true;
            if (!status.Connected && !status.WifiPaired && !status.UsbConnected)
            {
                SwitchView(showOnboarding: true);
            }
        }

        if (status.HistoryRevision != _lastHistoryRevision)
        {
            var historyResp = await _backend.GetHistoryAsync();
            if (historyResp is not null)
            {
                _lastHistoryRevision = historyResp.Revision;
                _allItems = historyResp.Items;
                NavCountText.Text = _allItems.Count.ToString();
                RenderHistoryCards();
            }
        }
    }

    private void UpdateStatusUi(BackendStatus status)
    {
        // Header Status Pill
        if (status.Connected)
        {
            HeaderStatusDot.Fill = BrushAppleGreen;
            var iosPart = string.IsNullOrEmpty(status.IosVersion) ? "" : $" · iOS {status.IosVersion}";
            var transportPart = string.IsNullOrEmpty(status.ActiveTransport) ? "" : $" · {status.ActiveTransport}";
            HeaderDeviceText.Text = $"{status.DeviceName}{iosPart}{transportPart}";

            BannerHeadlineText.Text = $"{status.DeviceName} Connected";
            BannerTransportPill.Background = BrushGreenTint;
            BannerTransportPill.BorderBrush = BrushGreenBorder;
            BannerTransportText.Foreground = BrushAppleGreen;
            BannerTransportText.Text = string.IsNullOrEmpty(status.ActiveTransport) ? "ACTIVE" : status.ActiveTransport.ToUpperInvariant();
            BannerSubtext.Text = "Listening to com.apple.pasteboard.notify.changed — copy anything on iPhone and press Ctrl + V on Windows.";
        }
        else
        {
            HeaderStatusDot.Fill = BrushAppleAmber;
            HeaderDeviceText.Text = status.StateMessage;

            BannerHeadlineText.Text = "Connecting to iPhone...";
            BannerTransportPill.Background = BrushAmberTint;
            BannerTransportPill.BorderBrush = BrushAmberBorder;
            BannerTransportText.Foreground = BrushAppleAmber;
            BannerTransportText.Text = status.State.ToUpperInvariant();
            BannerSubtext.Text = status.StateMessage;
        }

        MetricLatencyText.Text = status.LastSyncMs.HasValue ? $"{status.LastSyncMs.Value:F1} ms" : "— ms";
        MetricChangeCountText.Text = status.LastChangeCount.HasValue ? $"#{status.LastChangeCount.Value}" : "#—";

        FooterUdidText.Text = string.IsNullOrEmpty(status.Udid) ? "" : $"UDID {status.Udid}";
        FooterStatusText.Text = status.DdiMounted
            ? "CoreDevice PasteboardService · Cryptex1 Mounted · POLICY_PROMISE_SECONDARY"
            : "CoreDevice PasteboardService · Waiting for Developer Disk Image";

        // Transport Mode Buttons
        UpdateTransportSelectorUi(status.TransportMode);

        // Onboarding Step Cards Live Status
        var step1Ready = status.UsbConnected || status.WifiPaired || status.Connected;
        Step1StatusBadge.Background = step1Ready ? BrushGreenTint : BrushAmberTint;
        Step1StatusBadge.BorderBrush = step1Ready ? BrushGreenBorder : BrushAmberBorder;
        Step1StatusText.Foreground = step1Ready ? BrushAppleGreen : BrushAppleAmber;
        Step1StatusText.Text = step1Ready ? "✓ Trusted" : "Connect USB";
        if (step1Ready && !string.IsNullOrEmpty(status.Udid))
        {
            Step1DetailText.Text = $"Detected {status.DeviceName} (UDID {status.Udid}). CoreDevice lockdown pairing verified.";
        }

        var step2Ready = status.WifiPaired;
        Step2StatusBadge.Background = step2Ready ? BrushGreenTint : BrushBlueTint;
        Step2StatusBadge.BorderBrush = step2Ready ? BrushGreenBorder : BrushBlueBorder;
        Step2StatusText.Foreground = step2Ready ? BrushAppleGreen : BrushAppleBlue;
        Step2StatusText.Text = step2Ready ? "✓ Wi-Fi Ready" : "Optional";

        var step3Ready = status.Connected && status.DdiMounted;
        Step3StatusBadge.Background = step3Ready ? BrushGreenTint : BrushAmberTint;
        Step3StatusBadge.BorderBrush = step3Ready ? BrushGreenBorder : BrushAmberBorder;
        Step3StatusText.Foreground = step3Ready ? BrushAppleGreen : BrushAppleAmber;
        Step3StatusText.Text = step3Ready ? $"✓ Live ({status.ActiveTransport})" : "Connecting...";
    }

    private void UpdateTransportSelectorUi(string mode)
    {
        mode = (mode ?? "auto").ToLowerInvariant();
        TransportAutoBtn.Background = mode == "auto" ? BrushAppleBlue : BrushTransparent;
        TransportAutoBtn.Foreground = mode == "auto" ? BrushTextPrimary : BrushTextSecondary;

        TransportWifiBtn.Background = mode == "wifi" ? BrushAppleBlue : BrushTransparent;
        TransportWifiBtn.Foreground = mode == "wifi" ? BrushTextPrimary : BrushTextSecondary;

        TransportUsbBtn.Background = mode == "usb" ? BrushAppleBlue : BrushTransparent;
        TransportUsbBtn.Foreground = mode == "usb" ? BrushTextPrimary : BrushTextSecondary;
    }

    private void SwitchView(bool showOnboarding)
    {
        if (showOnboarding)
        {
            ClipboardManagerView.Visibility = Visibility.Collapsed;
            OnboardingView.Visibility = Visibility.Visible;
            NavOnboardingBtn.Background = BrushNavActive;
            NavClipboardBtn.Background = BrushTransparent;
            NavOnboardingText.Foreground = BrushTextPrimary;
            AnimateViewTransition(OnboardingView);
        }
        else
        {
            OnboardingView.Visibility = Visibility.Collapsed;
            ClipboardManagerView.Visibility = Visibility.Visible;
            NavClipboardBtn.Background = BrushNavActive;
            NavOnboardingBtn.Background = BrushTransparent;
            NavOnboardingText.Foreground = BrushTextSecondary;
            AnimateViewTransition(ClipboardManagerView);
        }
    }

    private void RenderHistoryCards()
    {
        HistoryListPanel.Children.Clear();

        var filtered = _allItems.Where(item =>
        {
            var matchesFilter = _activeFilter switch
            {
                "text" => item.Kind == "text",
                "url" => item.Kind == "url",
                "image" => item.Kind == "image",
                "pinned" => item.Pinned,
                _ => true,
            };
            if (!matchesFilter) return false;

            if (string.IsNullOrWhiteSpace(_searchQuery)) return true;
            var q = _searchQuery.Trim();
            return item.Text.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.Uti.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.Transport.Contains(q, StringComparison.OrdinalIgnoreCase);
        }).ToList();

        EmptyStateCard.Visibility = filtered.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        foreach (var item in filtered)
        {
            HistoryListPanel.Children.Add(BuildClipboardCard(item));
        }
    }

    private UIElement BuildClipboardCard(ClipboardHistoryItem item)
    {
        var card = new Border
        {
            Background = BrushCardSurface,
            BorderBrush = item.Pinned ? BrushBlueBorder : BrushCardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(13),
            Padding = new Thickness(18, 14, 18, 14),
        };

        card.PointerEntered += (_, _) => card.Background = BrushCardHover;
        card.PointerExited += (_, _) => card.Background = BrushCardSurface;
        AttachPressFeedback(card, 0.99f);

        var outerStack = new StackPanel { Spacing = 10 };

        // Top Row: Type Badge + Metadata + Action Buttons
        var topGrid = new Grid();
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        topGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var metaRow = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            VerticalAlignment = VerticalAlignment.Center,
        };

        // Kind Pill
        var (kindLabel, kindFg, kindBg) = item.Kind switch
        {
            "image" => ("IMAGE", BrushAppleGreen, BrushGreenTint),
            "url" => ("LINK", BrushApplePurple, BrushPurpleTint),
            _ => ("TEXT", BrushAppleBlue, BrushBlueTint),
        };

        var kindPill = new Border
        {
            Background = kindBg,
            CornerRadius = new CornerRadius(5),
            Padding = new Thickness(7, 2, 7, 2),
            Child = new TextBlock
            {
                Text = kindLabel,
                FontSize = 10.5,
                FontWeight = Microsoft.UI.Text.FontWeights.Bold,
                Foreground = kindFg,
            },
        };
        metaRow.Children.Add(kindPill);

        if (item.Pinned)
        {
            metaRow.Children.Add(new Border
            {
                Background = BrushAmberTint,
                CornerRadius = new CornerRadius(5),
                Padding = new Thickness(6, 2, 6, 2),
                Child = new TextBlock
                {
                    Text = "★ PINNED",
                    FontSize = 10,
                    FontWeight = Microsoft.UI.Text.FontWeights.Bold,
                    Foreground = BrushAppleGold,
                },
            });
        }

        var metaText = new TextBlock
        {
            Text = $"{item.Uti}  ·  {item.FormattedSize}  ·  {item.Transport}  ·  {item.LatencyMs:F1} ms  ·  {item.TimeLabel}",
            FontSize = 11.5,
            Foreground = BrushTextMuted,
            VerticalAlignment = VerticalAlignment.Center,
        };
        Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(metaText, FontNumeralAlignment.Tabular);
        metaRow.Children.Add(metaText);

        Grid.SetColumn(metaRow, 0);
        topGrid.Children.Add(metaRow);

        // Action Buttons (Copy, Open URL if Link, Pin, Delete)
        var actionsRow = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            VerticalAlignment = VerticalAlignment.Center,
        };

        var copyBtnText = new TextBlock
        {
            Text = "Copy",
            FontSize = 12,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            Foreground = BrushTextPrimary,
        };

        var copyBtn = new Button
        {
            Background = BrushBlueTint,
            BorderBrush = BrushBlueBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(7),
            Padding = new Thickness(12, 4, 12, 4),
            Content = copyBtnText,
        };
        AttachPressFeedback(copyBtn, 0.95f);

        async Task CopyAndIndicateAsync()
        {
            var ok = await _backend.CopyItemToWindowsAsync(item.Id);
            if (ok)
            {
                copyBtn.Background = BrushGreenTint;
                copyBtn.BorderBrush = BrushGreenBorder;
                copyBtnText.Foreground = BrushAppleGreen;
                copyBtnText.Text = "✓ Copied";
                await Task.Delay(1300);
                copyBtn.Background = BrushBlueTint;
                copyBtn.BorderBrush = BrushBlueBorder;
                copyBtnText.Foreground = BrushTextPrimary;
                copyBtnText.Text = "Copy";
            }
        }

        copyBtn.Click += async (_, _) => await CopyAndIndicateAsync();
        actionsRow.Children.Add(copyBtn);

        if (item.Kind == "url" && Uri.TryCreate(item.Text.Trim(), UriKind.Absolute, out var targetUri))
        {
            var openBtn = new Button
            {
                Content = "Open ↗",
                FontSize = 11.5,
                Background = BrushPillInactive,
                Foreground = BrushTextSecondary,
                BorderBrush = BrushCardBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(7),
                Padding = new Thickness(10, 4, 10, 4),
            };
            openBtn.Click += async (_, _) => await Windows.System.Launcher.LaunchUriAsync(targetUri);
            actionsRow.Children.Add(openBtn);
        }

        var pinBtn = new Button
        {
            Content = item.Pinned ? "★" : "☆",
            FontSize = 12.5,
            Background = BrushPillInactive,
            Foreground = item.Pinned ? BrushAppleGold : BrushTextSecondary,
            BorderBrush = BrushCardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(7),
            Padding = new Thickness(9, 3, 9, 3),
        };
        pinBtn.Click += async (_, _) =>
        {
            await _backend.TogglePinAsync(item.Id);
            _lastHistoryRevision = -1;
            await RefreshStatusAndHistoryAsync();
        };
        actionsRow.Children.Add(pinBtn);

        var deleteBtn = new Button
        {
            Content = "✕",
            FontSize = 11.5,
            Background = BrushPillInactive,
            Foreground = BrushTextMuted,
            BorderBrush = BrushCardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(7),
            Padding = new Thickness(9, 4, 9, 4),
        };
        deleteBtn.Click += async (_, _) =>
        {
            await _backend.DeleteItemAsync(item.Id);
            _lastHistoryRevision = -1;
            await RefreshStatusAndHistoryAsync();
        };
        actionsRow.Children.Add(deleteBtn);

        Grid.SetColumn(actionsRow, 1);
        topGrid.Children.Add(actionsRow);
        outerStack.Children.Add(topGrid);

        // Content Body (Image Thumbnail or Text/URL Preview)
        if (item.Kind == "image")
        {
            var imgContainer = new Border
            {
                Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x0E, 0x10, 0x17)),
                BorderBrush = BrushCardBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(8),
                HorizontalAlignment = HorizontalAlignment.Left,
            };

            var imgStack = new StackPanel { Spacing = 6 };
            try
            {
                var bitmap = new BitmapImage(new Uri(_backend.GetImageUrl(item.Id)));
                var imgControl = new Image
                {
                    Source = bitmap,
                    MaxHeight = 210,
                    MaxWidth = 520,
                    Stretch = Stretch.Uniform,
                    HorizontalAlignment = HorizontalAlignment.Left,
                };
                imgStack.Children.Add(imgControl);
            }
            catch
            {
                // Fallback if image stream not ready
            }

            var dimText = new TextBlock
            {
                Text = item.Width > 0 && item.Height > 0
                    ? $"{item.Width} × {item.Height} px  ·  Click Copy or press Ctrl + V to paste image"
                    : "Synced image from iPhone",
                FontSize = 12,
                Foreground = BrushTextSecondary,
            };
            Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(dimText, FontNumeralAlignment.Tabular);
            imgStack.Children.Add(dimText);

            imgContainer.Child = imgStack;
            outerStack.Children.Add(imgContainer);
        }
        else
        {
            var bodyText = new TextBlock
            {
                Text = item.Text,
                FontSize = 13.5,
                Foreground = item.Kind == "url" ? BrushAppleBlue : BrushTextPrimary,
                TextWrapping = TextWrapping.Wrap,
                MaxLines = 5,
                TextTrimming = TextTrimming.CharacterEllipsis,
                IsTextSelectionEnabled = true,
            };
            outerStack.Children.Add(bodyText);
        }

        card.Child = outerStack;
        return card;
    }

    private void OnNavClipboardClick(object sender, RoutedEventArgs e)
    {
        SwitchView(showOnboarding: false);
    }

    private void OnNavOnboardingClick(object sender, RoutedEventArgs e)
    {
        SwitchView(showOnboarding: true);
    }

    private async void OnSyncNowClick(object sender, RoutedEventArgs e)
    {
        await _backend.TriggerSyncNowAsync();
        await Task.Delay(250);
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnTransportAutoClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportSelectorUi("auto");
        await _backend.SetTransportModeAsync("auto");
    }

    private async void OnTransportWifiClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportSelectorUi("wifi");
        await _backend.SetTransportModeAsync("wifi");
    }

    private async void OnTransportUsbClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportSelectorUi("usb");
        await _backend.SetTransportModeAsync("usb");
    }

    private void OnFilterPillClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string filterTag)
        {
            return;
        }

        _activeFilter = filterTag;
        var buttons = new[] { FilterAllBtn, FilterTextBtn, FilterUrlBtn, FilterImageBtn, FilterPinnedBtn };
        foreach (var btn in buttons)
        {
            var isActive = ReferenceEquals(btn, clicked);
            btn.Background = isActive ? BrushAppleBlue : BrushPillInactive;
            btn.Foreground = isActive ? BrushTextPrimary : BrushTextSecondary;
            btn.BorderThickness = new Thickness(isActive ? 0 : 1);
        }

        RenderHistoryCards();
    }

    private void OnSearchTextChanged(object sender, TextChangedEventArgs e)
    {
        _searchQuery = SearchBox.Text ?? "";
        RenderHistoryCards();
    }

    private async void OnClearHistoryClick(object sender, RoutedEventArgs e)
    {
        await _backend.ClearHistoryAsync(includePinned: false);
        _lastHistoryRevision = -1;
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnPairWifiClick(object sender, RoutedEventArgs e)
    {
        PairWifiBtn.IsEnabled = false;
        PairWifiBtnText.Text = "Pairing over USB...";
        OnboardingFeedbackText.Text = "Establishing wireless RemotePairing record over USB...";

        var (ok, message) = await _backend.PairWifiOverUsbAsync();
        PairWifiBtn.IsEnabled = true;
        PairWifiBtnText.Text = ok ? "✓ Paired Wireless Sync" : "Retry Wireless Pairing";
        OnboardingFeedbackText.Foreground = ok ? BrushAppleGreen : BrushAppleRed;
        OnboardingFeedbackText.Text = message;

        await RefreshStatusAndHistoryAsync();
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        _pollTimer.Stop();
        _backend.Dispose();
    }
}
