variable "aws_region" {
  type    = string
  default = "ap-southeast-3"
}

variable "alert_email" {
  description = "The only address that receives alerts, reports and dashboard sign-in links."
  type        = string
  default     = "toshiki@agenasports.com"
}

variable "alert_from_address" {
  description = "Verified SES identity alerts are sent from (SES sandbox: a verified address)."
  type        = string
  default     = "toshiki@agenasports.com"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Public subnets for Fargate tasks (outbound internet: Claude, Meta, GitHub, HadeethEnc)."
  type        = list(string)
}

variable "db_subnet_ids" {
  description = "Private subnets (two AZs) for the database."
  type        = list(string)
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "publish_live" {
  description = "false: every job runs for real and records what it would post. true: posts to Instagram and Threads."
  type        = bool
  default     = false
}

variable "launch_mode" {
  description = "prelaunch (app in Play testing) or live. Flipping to live schedules the launch series and turns on comment→DM."
  type        = string
  default     = "prelaunch"
}

variable "play_store_url" {
  type    = string
  default = ""
}

variable "translation_mode" {
  description = "kemenag (Kemenag translation on slides) or makna (Arabic + reference + paraphrase)."
  type        = string
  default     = "kemenag"
}

variable "kill_switch" {
  description = "Hard stop for all publishing, independent of the dashboard flag."
  type        = bool
  default     = false
}

variable "schedules_enabled" {
  type    = bool
  default = false
}

variable "schedules_start" {
  type    = string
  default = null
}
