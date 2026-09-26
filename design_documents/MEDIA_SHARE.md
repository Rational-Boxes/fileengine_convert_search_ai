# Extension of external share to publish audio and video with or without gating

Enhance both the converter and external sharing subservice to publish
audio and video media. Add an operation to convert a video or audio
file to WEBM, extending the existing video preview system, to convert
the full video to WEBM at up tp 720P quality with full quality audio.
Convert audio with the highest quality streaming MP3 file.

## Integration to the sharing framework

Support both a direct share and an embeddable Web-Component that can embed in
any other website. The sharing needs to support three options: open-sharing to
any visitor, gated to the specified emails as per the existing sharing
framework, and allow anyone to access if they provide their email address without
needing the verification step. The email addresses need to be gathered and 
displayed in the history/status interface for the sharing UI on the folder drawer.
Likewise, the gathered email addresses need to be added to a CSV file in the
file's sidecar. This way fetching the sidecar file is a simple integration.