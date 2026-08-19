from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    print("Creator_Engagement_Project")
    print(f"项目目录: {PROJECT_ROOT}")
    print("单 URL: python -m app.manually_execute_script.fetch_url_engagement '<URL>'")
    print("API: uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8200")


if __name__ == "__main__":
    main()
