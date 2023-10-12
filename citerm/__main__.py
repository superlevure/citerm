import os
import asyncio
import subprocess
from time import sleep

from rich.text import Text

from textual import work, on
from textual.app import App
from textual.message import Message
from textual.widgets import Label, RichLog, LoadingIndicator
from textual.worker import get_current_worker

import gitlab
from gitlab import GitlabGetError


class CustomRichLog(RichLog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keep = False

    def key_w(self) -> None:
        self.remove()

    def key_k(self) -> None:
        self.keep = not self.keep

    def on_focus(self) -> None:
        self.styles.border_title_style = "bold"

    def on_blur(self) -> None:
        self.styles.border_title_style = "none"


class CitermApp(App):
    TITLE = "Citerm"
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = None
        self.job_count = 0

    def compose(self):
        yield LoadingIndicator()

    @work(thread=True, name="get_job_trace")
    def get_job_trace(self, job):
        log = self.query_one(f"#job_{job.id}")
        worker = get_current_worker()
        trace_len = 0

        while True:
            # self.call_from_thread(log.write, "Checking..")
            job.refresh()
            current_trace = job.trace(obey_rate_limit=False).decode("utf-8")

            if worker.is_cancelled:
                self.log(f"Job {job.id} cancelled")
                return

            if len(current_trace) > trace_len:
                if not worker.is_cancelled:
                    self.call_from_thread(
                        log.write, Text.from_ansi(current_trace[trace_len:])
                    )
                    trace_len = len(current_trace)
                else:
                    self.log(f"Job {job.id} cancelled")
                    return

            if job.status == "success":
                log.styles.border = ("solid", "green")
                self.log(f"Job {job.id} success")
                if not log.keep:
                    sleep(5)
                    self.call_from_thread(log.remove)
                    self.job_count -= 1
                return

            elif job.status == "failed":
                self.log(f"Job {job.id} failed")
                log.styles.border = ("solid", "red")
                return

            self.log(f"Job {job.id} - {job.name} [{job.status}] checking..")
            sleep(0.1)

    @work(thread=True, name="handle_project")
    async def handle_project(self):
        gl = gitlab.Gitlab(
            "https://gitlab.com",
            private_token=os.getenv("GITLAB_TOKEN"),
        )
        project_namespace = self._get_project_namespace()
        try:
            self.project = gl.projects.get(project_namespace)
        except GitlabGetError:
            self.exit(f"Project [{gl.url}/{project_namespace}] not found")

        current_branch = self._run_command("git symbolic-ref --quiet --short HEAD")
        pipeline = self.project.pipelines.list(ref=current_branch, page=1, per_page=1)[0]

        ALLOWED_STATUSES = [
            "failed",
            "warning",
            "pending",
            "running",
            # "manual",
            "scheduled",
            # "canceled",
            # "success",
            "skipped",
            "created",
        ]
        jobs = pipeline.jobs.list(
            get_all=True,
            include_retried=True,
        )
        jobs = [job for job in jobs if job.status in ALLOWED_STATUSES]

        self.post_message(self.JobsRetrieved(jobs))

    class JobsRetrieved(Message):
        def __init__(self, jobs) -> None:
            self.jobs = jobs
            super().__init__()

    @on(JobsRetrieved)
    async def handle_jobs_retrieved(self, message: Message):
        jobs = message.jobs

        self.query_one(LoadingIndicator).display = False
        asyncio.create_task(self.jobs_worker(jobs))

    async def jobs_worker(self, jobs):
        MAX_JOBS = 5
        self.job_count = 0

        while len(jobs):
            for job in jobs[::-1]:
                job = self.project.jobs.get(job.id)
                self.log(f"QUEUE: Job {job.id} - {job.name} [{job.status}]")

                if self.job_count < MAX_JOBS and job.status in [
                    "running",
                    "failed",
                    "success",
                ]:
                    self.job_count += 1
                    jobs.remove(job)
                    self.log(f"Starting job {job.id}")
                    log = CustomRichLog(
                        id=f"job_{job.id}",
                        classes="log",
                        auto_scroll=True,
                    )
                    log.border_title = job.name
                    await self.mount(log)
                    self.get_job_trace(job)
                else:
                    await asyncio.sleep(0.5)

        self.mount(Label("Pipeline is done!"))

    async def on_ready(self):
        self.handle_project()

    def _get_project_namespace(self) -> str:
        remotes = self._run_command("git remote -v")
        return (
            remotes.split("\n")[0]
            .split("\t")[1]
            .split(" ")[0]
            .split(":")[1]
            .removesuffix(".git")
        )

    def _run_command(self, command) -> str:
        result = subprocess.run(
            command.split(" "),
            capture_output=True,
            check=True,
        )
        return result.stdout.decode("utf-8").strip()


def main():
    app = CitermApp(css_path="citerm.tcss")
    if exit_message := app.run():
        print(exit_message)


if __name__ == "__main__":
    main()
