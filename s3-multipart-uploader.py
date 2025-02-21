#!/usr/bin/env pytyhon3
from __future__ import annotations

import hashlib
import logging
import math
import sys
import time
import os
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import boto3
import botocore.exceptions

# https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.create_multipart_upload

# S3 API limitations
MAX_ALLOWED_PART_COUNT_S3 = 10_000
MIN_REQUIRED_PART_SIZE_S3 = 5 * 1024 * 1024

BLOCKSIZE_FOR_HASHING = 1024 * 1024


LOG = logging.getLogger()


def parse_args() -> Tuple[ArgumentParser, Namespace]:
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation")

    upload_parser = subparsers.add_parser("upload", aliases=["up", "u"])
    upload_parser.add_argument("--upload_id", metavar="UPLOAD_ID", type=str, default="",
                               help="UploadId from previously started upload")
    upload_parser.add_argument("file", metavar="<File>", type=str, help="Local file path to upload")
    upload_parser.add_argument("bucket", metavar="<Bucket>", type=str,
                               help="Destination S3 Bucket")
    upload_parser.add_argument("key", metavar="<Key>", type=str, nargs="?",
                               help="Destination S3 Key (optional, will use filename without path of <File>)")
    upload_parser.add_argument("--part-size", type=int, default=None,
                               help="Part size for upload in bytes")

    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument("--all", action="store_true",
                              help="Abort *all* existing multipart uploads for bucket and key")
    abort_parser.add_argument("--list", action="store_true",
                              help="List existing multipart uploads for bucket and key")
    abort_parser.add_argument("file", metavar="<File>", type=str,
                               help="Local file path to upload (used only to determine key for abort)")
    abort_parser.add_argument("bucket", metavar="<Bucket>", type=str,
                               help="Destination S3 Bucket")
    abort_parser.add_argument("key", metavar="<Key>", type=str,
                               help="Destination S3 Key")
    abort_parser.add_argument("--upload_id", type=str, default="",
                              help="UploadId to abort, from previously started upload")

    return parser, parser.parse_args()


class OutFormatter:
    def __init__(self):
        try:
            import humanize
            self._format_sizesize = lambda size: humanize.filesize.naturalsize(size)
            self._format_secs = lambda secs: humanize.precisedelta(secs)
        except ModuleNotFoundError:
            self._format_sizesize = None
            self._format_secs = None

    def filesize(self, size: int) -> str:
        if self._format_sizesize:
            return self._format_sizesize(size) + f" ({size})"
        return f"{size}"

    def time(self, seconds: int) -> str:
        if self._format_secs:
            return self._format_secs(seconds)
        return f"{seconds}s"


class ProgressMeter:
    def __init__(self, label: str, limit: int, auto_status_secs: Optional[int] = 1, initial: int = 0,
                 out_formatter: OutFormatter = OutFormatter()):
        self.label = label
        self.limit = limit
        self.initial = initial
        self.current = initial
        self.auto_status_secs = auto_status_secs
        self.start_time = time.perf_counter()
        self.last_update_time = self.start_time
        self._out_formatter = out_formatter

    def increment(self, by: int = 1):
        self.current = min(self.current + by, self.limit)
        delta = time.perf_counter() - self.last_update_time
        self.last_update_time = time.perf_counter()
        if delta > self.auto_status_secs:
            self.log_status()

    def log_status(self):
        percent = 100 * self.current / self.limit
        time_passed_secs = self.last_update_time - self.start_time
        estimated_remaining_time_secs = (self.limit - self.current) / (self.current - self.initial) * time_passed_secs \
            if self.current - self.initial > 0 else -1
        LOG.info(f"{self.label} {percent:.2f}% "
                 f"(elapsed: {self._out_formatter.time(int(time_passed_secs))}, ETR: ~{self._out_formatter.time(int(estimated_remaining_time_secs))})"
                 f"‚ ({self.current}/{self.limit})")

    def __enter__(self) -> ProgressMeter:
        LOG.info(f"Started: {self.label}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.log_status()
        LOG.info(f"Finished: {self.label}")


@dataclass
class UploadedPart:
    number: int
    last_modified: str
    size: int
    etag: str


@dataclass
class MultipartUploadInProgress:
    started_at: str
    upload_id: str
    initiator: str
    parts: List[UploadedPart] = None


class S3MultipartUploader:
    def __init__(
            self,
            bucket: str,
            filepath_to_upload: Path,
            dest_key: str,
            part_size_in_bytes: Optional[int],
            args: Namespace,  # TODO: remove args from here
            out_formatter: OutFormatter = OutFormatter(),
    ):
        self._out_formatter = out_formatter
        self.part_size_in_bytes = part_size_in_bytes
        self.dest_key = dest_key
        self.filepath_to_upload = filepath_to_upload
        self.bucket = bucket

        self.args = args

        self.compute_md5 = False
        self.filesize = self.filepath_to_upload.stat().st_size
        self.part_count: Optional[int] = None
        self._determine_part_count()
        self.s3 = boto3.client("s3")

    def check_existing_uploads(self) -> Optional[MultipartUploadInProgress]:
        expected_part_count = self.part_count

        response = self.s3.list_multipart_uploads(
            Bucket=self.bucket
        )
        LOG.debug(response)
        matching = [MultipartUploadInProgress(
            upload_id=upload["UploadId"],
            started_at=upload["Initiated"],
            initiator=upload["Initiator"]["ID"])
            for upload in response.get("Uploads", []) if upload["Key"] == self.dest_key]
        uploads_for_key = len(matching)

        upload_id = self.args.upload_id
        if upload_id:
            matching = [mpupload for mpupload in matching if mpupload.upload_id == upload_id]

        num_matches = len(matching)
        if num_matches == 0:
            if upload_id:
                LOG.error(
                    f"No upload found for upload_id {upload_id} (found {uploads_for_key} total uploads for this bucket and key)")
                exit(1)
            else:
                LOG.info(f"No uploads found for this bucket and key")
                return None
        if num_matches > 1:
            if upload_id:
                LOG.error(f"Found {num_matches} uploads for this upload_id - this is probably a bug")
            else:
                LOG.error(f"Found {num_matches} uploads in progress, please specify an upload_id")
            exit(1)
        match = matching[0]
        upload_id = match.upload_id
        LOG.info(
            f"Found existing upload started at {match.started_at} by {match.initiator}, upload_id: \"{upload_id}\"")

        parts = []
        new_parts, is_truncated, next_part_number_marker = self._load_parts(upload_id=upload_id, part_number_marker=None)
        parts.extend(new_parts)

        while is_truncated:
            new_parts, is_truncated, next_part_number_marker = self._load_parts(upload_id=upload_id, part_number_marker=next_part_number_marker)
            parts.extend(new_parts)

        num_parts = len(parts)
        LOG.info(f"Found {num_parts} uploaded parts")

        return MultipartUploadInProgress(
            upload_id=match.upload_id,
            started_at=match.started_at,
            initiator=match.initiator,
            parts=parts,
        )

    def _load_parts(self, upload_id: str, part_number_marker: Optional[int]) -> Tuple[List[UploadedPart], bool, Optional[int]]:
        if part_number_marker is None:
            parts_response = self.s3.list_parts(Bucket=self.bucket, Key=self.dest_key, UploadId=upload_id, MaxParts=1000)
        else:
            parts_response = self.s3.list_parts(Bucket=self.bucket, Key=self.dest_key, UploadId=upload_id, MaxParts=1000,
                                                PartNumberMarker=part_number_marker)
        LOG.debug(parts_response)
        next_part_number_marker = parts_response["NextPartNumberMarker"] if "NextPartNumberMarker" in parts_response else None
        is_truncated = "IsTruncated" in parts_response and parts_response["IsTruncated"]
        new_parts = [
                UploadedPart(
                    number=part["PartNumber"],
                    last_modified=part["LastModified"],
                    size=part["Size"],
                    etag=part["ETag"],
                )
                for part in parts_response.get("Parts", [])
            ]
        LOG.debug(f"Found {len(new_parts)} parts from response, next PartNumberMarker={next_part_number_marker}")
        return new_parts, is_truncated, next_part_number_marker

    def upload(self):
        if self._is_existing_in_s3():
            LOG.info(f"Skip file already existing in S3 {self.filepath_to_upload}")
            return

        upload = self.check_existing_uploads()
        if upload:
            # TODO: add option to progress non-interactively
            should_continue = input("Continue existing upload [yN]? ") == "y"
            if should_continue:
                self._continue_upload(upload)
                return
            else:
                should_abort = input("Delete existing multi-part upload [yN]? ") == "y"
                if should_abort:
                    raise Exception("Not yet implemented")

                should_restart = input("Re-start multi-part upload from scratch [yN]? ") == "y"
                if not should_restart:
                    return
                LOG.info("Starting new upload")

        upload_id = self._create_multipart_upload()

        uploaded_parts = self._upload_parts(start_index=0, upload_id=upload_id)

        self._finalize_upload(parts=uploaded_parts, upload_id=upload_id)

    def _is_existing_in_s3(self) -> bool:
        try:
            response = self.s3.head_object(Bucket=self.bucket, Key=self.dest_key)
            return True
        except botocore.exceptions.ClientError:
            return False

    def _create_multipart_upload(self):
        LOG.info(f"Create new multipart upload for {self.filepath_to_upload} to s3://{self.bucket}/{self.dest_key}")
        LOG.info(f"Will use {self.part_count} parts of max. size {self._out_formatter.filesize(self.part_size_in_bytes)}")

        if self.compute_md5:
            LOG.info("Computing md5...")
            md5 = self.compute_md5(self.filepath_to_upload)
            LOG.info(f"md5={md5}")
        else:
            md5 = None

        metadata = {"md5": md5} if md5 is not None else {}
        create_response = self.s3.create_multipart_upload(
            Bucket=self.bucket,
            Key=self.dest_key,
            Metadata=metadata,
        )
        LOG.debug(create_response)
        upload_id = create_response["UploadId"]
        LOG.info(f"Got upload_id={upload_id}")
        return upload_id

    def _finalize_upload(self, *, parts: List[UploadedPart], upload_id: str):
        total_bytes = sum([part.size for part in parts])
        LOG.debug(f"Total upload size: {self._out_formatter.filesize(total_bytes)}, expected: {self._out_formatter.filesize(self.filesize)}")
        if self.filesize != total_bytes:
            LOG.error("Mismatch of sizes - most likely, the file was changed since the first multi-part upload. "
                      "It is highly recommended to check the upload, as it is most likely incorrect!")
        complete_response = self.s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.dest_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {
                        "PartNumber": part.number,
                        "ETag": part.etag,
                    } for part in parts
                ]
            }
        )
        LOG.debug(complete_response)
        LOG.info("Completed multi-part upload")

    def _upload_parts(self, start_index: int, upload_id: str):
        uploaded_parts = []
        with ProgressMeter("Uploading parts", initial=start_index, limit=self.part_count) as progress:
            for part_index in range(start_index, self.part_count):
                start_offset = min(part_index * self.part_size_in_bytes, self.filesize - 1)
                end_offset = min((part_index + 1) * self.part_size_in_bytes - 1, self.filesize - 1)
                this_part_size = end_offset - start_offset + 1

                part_number = part_index + 1  # part numbers in S3 start at 1
                LOG.debug(
                    f"Part #{part_number}/{self.part_count} (byte offset {start_offset}-{end_offset}, size={self._out_formatter.filesize(this_part_size)})")

                with self.filepath_to_upload.open("rb") as file:
                    file.seek(start_offset)
                    data = file.read(self.part_size_in_bytes)  # if EOF is reached, this returns less for the last part

                if len(data) != this_part_size:
                    raise Exception(f"Did not read expected {this_part_size} but {len(data)}")

                upload_response = self.s3.upload_part(
                    Bucket=self.bucket,
                    Key=self.dest_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    ContentLength=this_part_size,
                    Body=data,
                )
                LOG.debug(upload_response)
                uploaded_parts.append(UploadedPart(number=part_number, last_modified="", size=this_part_size,
                                                   etag=upload_response["ETag"]))
                progress.increment()
        LOG.debug(uploaded_parts)

        uploaded_bytes = sum([part.size for part in uploaded_parts])
        LOG.info(f"Uploaded size: {self._out_formatter.filesize(uploaded_bytes)}")

        return uploaded_parts

    @staticmethod
    def compute_md5(filepath_to_upload: Path):
        hasher = hashlib.md5()
        file_size = filepath_to_upload.stat().st_size
        with filepath_to_upload.open("rb") as file:
            with ProgressMeter("Computing file hash", limit=file_size) as progress:
                r = file.read(BLOCKSIZE_FOR_HASHING)
                while r:
                    hasher.update(r)
                    progress.increment(BLOCKSIZE_FOR_HASHING)
                    r = file.read(BLOCKSIZE_FOR_HASHING)
        md5 = hasher.hexdigest()
        return md5

    def _continue_upload(self, upload: MultipartUploadInProgress):
        LOG.debug("Sanity check on existing parts")
        existing_parts = sorted(upload.parts, key= lambda part: part.number)

        for index, part in enumerate(existing_parts):
            expected_part_number = index + 1  # part numbers start from 1
            if part.number != expected_part_number:
                # we don't support missing part numbers (i.e. starting from parts 1,2,4)
                LOG.error(f"Incorrect part number for part {part.number}. "
                          f"Cannot continue this upload.")
                exit(3)
            if part.size != self.part_size_in_bytes and index != len(existing_parts) - 1:
                # different size allowed only for last part
                LOG.error(f"Incorrect part size for part {part.number} (got: {part.size}, "
                          f"expected: {self.part_size_in_bytes})."
                          f"Cannot continue this upload.")
                exit(3)

        start_index = len(existing_parts)
        start_part_number = start_index + 1
        LOG.debug(f"Parts look consistent, will continue from part {start_part_number}")

        upload_id = upload.upload_id

        new_uploaded_parts = self._upload_parts(start_index=start_index, upload_id=upload_id)
        all_parts = existing_parts + new_uploaded_parts
        self._finalize_upload(parts=all_parts, upload_id=upload_id)

    def _determine_part_count(self):
        if not self.part_size_in_bytes:
            self.part_size_in_bytes = max(math.ceil(self.filesize / MAX_ALLOWED_PART_COUNT_S3), MIN_REQUIRED_PART_SIZE_S3)
            LOG.info(f"Auto determined part size to {self._out_formatter.filesize(self.part_size_in_bytes)}")

        self.part_count = math.ceil(self.filesize / self.part_size_in_bytes)

        if self.part_size_in_bytes < MIN_REQUIRED_PART_SIZE_S3:
            LOG.fatal(f"Part size is smaller than minimum ({self.part_size_in_bytes}/{MIN_REQUIRED_PART_SIZE_S3}), "
                      f"please choose a larger part size")
            exit(1)
        if self.part_count > MAX_ALLOWED_PART_COUNT_S3:
            LOG.fatal(f"Part count required exceeds limit ({self.part_count}/{MAX_ALLOWED_PART_COUNT_S3}), "
                      f"please choose a larger part size")
            exit(1)

    def abort(self):
        if self.args.all or self.args.list:
            response = self.s3.list_multipart_uploads(
                Bucket=self.bucket
            )
            LOG.debug(response)
            matching = [MultipartUploadInProgress(
                upload_id=upload["UploadId"],
                started_at=upload["Initiated"],
                initiator=upload["Initiator"]["ID"])
                for upload in response.get("Uploads", []) if upload["Key"] == self.dest_key or self.dest_key is None]

            if len(matching) > 0:
                print(f"Found existing uploads for bucket {self.bucket} and key {self.dest_key}:")
                for mpupload in matching:
                    print(f"* {mpupload.upload_id}   {mpupload.started_at}   {mpupload.initiator}")
            else:
                print(f"No existing uploads for bucket {self.bucket} and key {self.dest_key}")

            if self.args.all:
                LOG.info(f"Aborting {len(matching)} uploads for s3://{self.bucket}/{self.dest_key} ...")
                for mpupload in matching:
                    LOG.info(f"Aborting {mpupload.upload_id}")
                    self.s3.abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=self.dest_key,
                        UploadId=mpupload.upload_id,
                    )
        else:
            LOG.debug(self.s3.abort_multipart_upload(
                Bucket=self.bucket,
                Key=self.dest_key,
                UploadId=self.args.upload_id,
            ))


class S3MultiPartCli:
    def run(self):
        parser, args = parse_args()

        LOG.debug(args)

        filepath_name_to_upload = args.file
        filepath_to_upload = Path(filepath_name_to_upload)

        if filepath_to_upload.is_dir():
            LOG.debug("Directory mode")

            if not args.key:
                print("Destination key (prefix) is required for directories")
                parser.print_usage()
                exit(2)

            for filepath in sorted(filepath_to_upload.iterdir()):
                if not filepath.is_file():
                    LOG.debug(f"Skipping non-file {filepath}")
                    continue
                dest_key = f"{args.key}/{filepath.name}"
                self._upload_file(filepath, dest_key, args)

        elif filepath_to_upload.is_file():
            dest_key = args.key or filepath_to_upload.name
            if not dest_key:
                print("Could not infer key from input file")
                parser.print_usage()
                exit(2)

            self._upload_file(filepath_to_upload, dest_key, args)
        else:
            print(f"Not a file or dir: {filepath_to_upload}")
            parser.print_usage()
            exit(2)

    def _upload_file(self, filepath_to_upload: Path, dest_key: str, args: Namespace):
        LOG.debug(f"Upload {filepath_to_upload} to {dest_key}")
        part_size = args.part_size

        s3_multipart_uploader = S3MultipartUploader(
            bucket=args.bucket,
            filepath_to_upload=filepath_to_upload,
            dest_key=dest_key,
            part_size_in_bytes=part_size,
            args=args,
        )

        operation = args.operation
        if operation in ["upload", "up", "u"]:
            operation = "upload"
        s3_multipart_uploader.__getattribute__(operation)()


def init_logging():
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(stream=sys.stdout, level=log_level)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("botocore.utils").setLevel(logging.WARNING)


if __name__ == "__main__":
    init_logging()
    s3_cli = S3MultiPartCli()
    s3_cli.run()
