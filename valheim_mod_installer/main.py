from .app import ValheimModDownloader


def main() -> None:
    app = ValheimModDownloader()
    app.mainloop()


if __name__ == "__main__":
    main()
