# -*- coding: utf-8 -*-
#
# This file is part of PyGaze - the open-source toolbox for eye tracking
#
# PyGaze is a Python module for easily creating gaze contingent experiments
# or other software (as well as non-gaze contingent experiments/software)
# Copyright (C) 2012-2013 Edwin S. Dalmaijer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>


# TobiiGlassesTracker
import copy
import math
import numpy as np


from pygaze import settings
from pygaze.libtime import clock
import pygaze
from pygaze.screen import Screen
from pygaze.keyboard import Keyboard
from pygaze.sound import Sound

from pygaze._eyetracker.baseeyetracker import BaseEyeTracker
# we try importing the copy_docstr function, but as we do not really need it
# for a proper functioning of the code, we simply ignore it when it fails to
# be imported correctly
try:
	from pygaze._misc.misc import copy_docstr
except:
	pass


import os
import datetime

import signal
import sys


import urllib2
import json
import time
import threading
import socket
import uuid
import logging as log

import warnings
warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)


# # # # #
# TobiiGlassesController

from tobiiglasses.tobiiglassescontroller import TobiiGlassesController

# # # # #
# functions

def calculate_angular_velocity(gp3_samples, eye_samples):
	""" Returns the angular velocity of eye movement

	The calculation is based on Tobii's I-VT filter. The law of cosines is used
	to get the angle between two gaze positions and the angular velocity is the
	angle between samples divided by the time difference.

	arguments
	gp3_samples		-- Gaze 3d position samples from tobii glasses
	eye_samples			-- Eye position samples from tobii glasses 

	returns
	angular_velocity	-- Angular velocity as a float number
	"""

	a = np.array(gp3_samples[0]['gp3']) - np.array(eye_samples[0]['pc']) 
	b = np.array(gp3_samples[-1]['gp3']) - np.array(eye_samples[-1]['pc'])
	c = np.array(gp3_samples[-1]['gp3']) - np.array(gp3_samples[0]['gp3'])

	# convert to norm/magnitude of vectors
	a = np.linalg.norm(a)
	b = np.linalg.norm(b)
	c = np.linalg.norm(c)

	# angle in degrees
	angle = np.degrees(np.arccos((a**2 + b**2 - c**2) / (2*a*b)))

	time_diff = gp3_samples[-1]['ts'] - gp3_samples[0]['ts']

	return math.abs(angle / time_diff)




# # # # #
# classes

class TobiiGlassesTracker(BaseEyeTracker):

	"""A class for Tobii Pro Glasses 2 EyeTracker objects"""

	def __init__(self, display, address='192.168.71.50', udpport=49152, logfile=settings.LOGFILE,
		eventdetection=settings.EVENTDETECTION, saccade_velocity_threshold=35,
		saccade_acceleration_threshold=9500, blink_threshold=settings.BLINKTHRESH, **args):

		"""Initializes a TobiiProGlassesTracker instance

		arguments
		display	--	a pygaze.display.Display instance

		keyword arguments
		address	-- internal ipv4/ipv6 address for Tobii Pro Glasses 2 (default=
				   '192.168.71.50', for IpV6 address use square brackets 
				   [fe80::xxxx:xxxx:xxxx:xxxx])
		udpport	-- UDP port number for Tobii Pro Glasses data streaming 
				   (default = 49152)
		"""

		# try to copy docstrings (but ignore it if it fails, as we do
		# not need it for actual functioning of the code)
		try:
			copy_docstr(BaseEyeTracker, TobiiProGlassesTracker)
		except:
			# we're not even going to show a warning, since the copied
			# docstring is useful for code editors; these load the docs
			# in a non-verbose manner, so warning messages would be lost
			pass


		# object properties
		self.disp = display
		self.screen = Screen()
		self.dispsize = settings.DISPSIZE		# display size in pixels
		self.screensize = settings.SCREENSIZE	# display size in cm
		self.screendist = settings.SCREENDIST	# distance between participant
												# and screen in cm
		self.pixpercm = (self.dispsize[0]/float(self.screensize[0]) + \ 
						self.dispsize[1]/float(self.screensize[1])) / 2.0
		self.kb = Keyboard(keylist=['space', 'escape', 'q'], timeout=1)
		self.errorbeep = Sound(osc='saw',freq=100, length=100)

		# output file properties
		self.outputfile = logfile
		self.description = "experiment" # TODO: EXPERIMENT NAME
		self.participant = "participant" # TODO: PP NAME

		# eye tracker properties
		self.eye_used = 0 # 0=left, 1=right, 2=binocular
		self.left_eye = 0
		self.right_eye = 1
		self.binocular = 2


		self.maxtries = 100 # number of samples obtained before giving up (for
							# obtaining accuracy and tracker distance 
							# information, as well as starting or stopping 
							# recording)
		self.prevsample = (-1,-1)

		# validation properties
		self.nvalsamples = 1000 # samples for one validation point

		# event detection properties
		self.fixtresh = 1.5 # degrees; maximal distance from fixation start
							# (if gaze wanders beyond this, fixation has 
							# stopped)

		self.fixtimetresh = 100	# milliseconds; amount of time gaze has to 
								# linger within self.fixtresh to be marked 
								# as a fixation

		self.spdtresh = saccade_velocity_threshold  # degrees per second; 
													# saccade velocity 
													# threshold

		self.accthresh = saccade_acceleration_threshold # degrees per second**2
														# saccade acceleration 
														# threshold

		self.blinkthresh = blink_threshold	# milliseconds; blink detection 
											# threshold used in PyGaze method
		self.eventdetection = eventdetection
		self.set_detection_type(self.eventdetection)
		self.weightdist = 10 # weighted distance, used for determining whether 
							 # a movement is due to measurement error (1 is ok,
							 # higher is more conservative and will result in 
							 # only larger saccades to be detected)


		self.tobiiglasses = TobiiGlassesController(udpport, address)

		self.triggers_values = {}

		self.logging = False
		self.current_recording_id = None
		self.current_participant_id = None
		self.current_project_id = None

		# Fixation filer parameters
		self.init_run = True			# Used for initial fixation run to 
										# collect samples to fill window

		self.num_fixation_samples = 3	# Default = 3. Can be increased to 
										# create a bigger window

		self.gaze_samples = []			# gaze position samples
		self.gp3_samples = []			# 3d gaze position samples
		self.eye_samples = []			# 3d eye position samples

		self.velocity_threshold = 70	# Degrees/second. 70 retains good 
										# accuracy for saccades and fixations

		self.latest_fixation = {}		# Latest recorded fixation.

		self.adjacent_threshold = 0.5	# Degrees. If angular difference of 
										# adjacent fixations are below
										# threshold, they are classified as the
										# belonging to the same fixation.

	def __del__(self):

		self.close()

	def __get_log_row__(self, keys, triggers):

		row = ""
		ac = [None, None, None]
		gy = [None, None, None]
		if "mems" in keys:

			try:
				for i in range(0,3):
					ac[i] = self.tobiiglasses.data['mems']['ac']['ac'][i]
			except:
				pass

			try:
				for i in range(0,3):
					gy[i] = self.tobiiglasses.data['mems']['gy']['gy'][i]
			except:
				pass

			row += ("%s; %s; %s; %s; %s; %s; " % (ac[0], ac[1], ac[2], gy[0], gy[1], gy[2]))


		gp = [None, None]
		if "gp" in keys:

			try:
				for i in range(0,2):
					gp[i] = self.tobiiglasses.data['gp']['gp'][i]
			except:
				pass

			row += ("%s; %s; " % (gp[0], gp[1]))


		gp3 = [None, None, None]
		if "gp3" in keys:

			try:
				for i in range(0,3):
					gp3[i] = self.tobiiglasses.data['gp3']['gp3'][i]
			except:
				pass

			row += ("%s; %s; %s; " % (gp3[0], gp3[1], gp3[2]))


		pc = [None, None, None]
		pd = None
		gd = [None, None, None]
		if "left_eye" in keys:

			try:
				for i in range(0,3):
					pc[i] = self.tobiiglasses.data['left_eye']['pc']['pc'][i]
			except:
				pass

			try:
				pd = self.tobiiglasses.data['left_eye']['pd']['pd']
			except:
				pass

			try:
				for i in range(0,3):
					gd[i] = self.tobiiglasses.data['left_eye']['gd']['gd'][i]
			except:
				pass


			row += ("%s; %s; %s; %s; %s; %s; %s; " % (pc[0], pc[1], pc[2], pd, gd[0], gd[1], gd[2]))

		pc = [None, None, None]
		pd = None
		gd = [None, None, None]
		if "right_eye" in keys:

			try:
				for i in range(0,3):
					pc[i] = self.tobiiglasses.data['right_eye']['pc']['pc'][i]
			except:
				pass

			try:
				pd = self.tobiiglasses.data['right_eye']['pd']['pd']
			except:
				pass

			try:
				for i in range(0,3):
					gd[i] = self.tobiiglasses.data['right_eye']['gd']['gd'][i]
			except:
				pass

			row += ("%s; %s; %s; %s; %s; %s; %s; " % (pc[0], pc[1], pc[2], pd, gd[0], gd[1], gd[2]))

		if len(triggers) > 0:
			for trigger in triggers:
				row += ("%s; " % self.triggers_values[trigger])

		row = row[:-2]
		return row

	def __get_log_header__(self, keys, triggers):

		header = "ts; "

		if "mems" in keys:
			header+="ac_x [m/s^2]; ac_y [m/s^2]; ac_z [m/s^2]; gy_x [°/s]; gy_y [°/s]; gy_z [°/s]; "
		if "gp" in keys:
			header+="gp_x; gp_y; "
		if "gp3" in keys:
			header+="gp3_x [mm]; gp3_y [mm]; gp3_z [mm]; "
		if "left_eye" in keys:
			header+="left_pc_x [mm]; left_pc_y [mm]; left_pc_z [mm]; left_pd [mm]; left_gd_x; left_gd_y; left_gd_z; "
		if "left_eye" in keys:
			header+="right_pc_x [mm]; right_pc_y [mm]; right_pc_z [mm]; right_pd [mm]; right_gd_x; right_gd_y; right_gd_z; "

		if len(triggers) > 0:
			for trigger in triggers:
				header+=trigger + "; "
				self.triggers_values[trigger] = None

		header = header[:-2]
		return header


	def __data_logger__(self, logfile, frequency, keys, triggers, time_offset):

		with open(logfile, 'a') as f:

			header = self.__get_log_header__(keys, triggers)
			f.write(header + "\n")

			while self.logging:

				row = self.__get_log_row__(keys, triggers)
				f.write("%s; %s \n" % (time_offset, row))
				time_period = float(1.0/float(frequency))
				time_offset += int(time_period*1000)
				time.sleep(time_period)


	def start_capturing(self):

		if not self.tobiiglasses.is_streaming():
			self.tobiiglasses.start_streaming()
		else:
			log.error("The eye-tracker is already in capturing mode.")

		return self.tobiiglasses.is_streaming()


	def stop_capturing(self):

		if self.tobiiglasses.is_streaming():
			self.tobiiglasses.stop_streaming()
		else:
			log.error("The eye-tracker is not in capturing mode.")

		return not self.tobiiglasses.is_streaming()


	def calibrate(self, calibrate=True, validate=True):

		"""Calibrates the eye tracking system

		arguments
		None

		keyword arguments
		calibrate	--	Boolean indicating if calibration should be
					performed (default = True)
		validate	--	Boolean indicating if validation should be performed
					(default = True)

		returns
		success	--	returns True if calibration succeeded, or False if
					not; in addition a calibration log is added to the
					log file and some properties are updated (i.e. the
					thresholds for detection algorithms)
		"""

		if self.current_project_id is None:
			self.current_project_id = self.set_project()

		if self.current_participant_id is None:
			self.current_participant_id = self.create_participant(self.current_project_id)

		calibration_id = self.__create_calibration__(self.current_project_id, self.current_participant_id)

		self.tobiiglasses.start_calibration(calibration_id)

		res = self.tobiiglasses.wait_until_is_calibrated(calibration_id)

		return res


	def set_current_project(self, project_name = None):

		if project_name is None:
			self.current_project_id = self.tobiiglasses.create_project()
		else:
			self.current_project_id = self.tobiiglasses.create_project(project_name)


	def set_current_participant(self, participant_name = None):

		if self.current_project_id is None:
			log.error("There is no project to assign a participant.")
		else:
			if participant_name is None:
				self.current_participant_id = self.tobiiglasses.create_participant(self.current_project_id)
			else:
				self.current_participant_id = self.tobiiglasses.create_participant(self.current_project_id, participant_name)


	def __create_calibration__(self, project_id, participant_id):

		calibration_id = self.tobiiglasses.create_calibration(project_id, participant_id)
		return calibration_id


	def start_logging(self, logfile, frequency, keys = ["mems", "gp", "gp3", "left_eye", "right_eye"], triggers = [], time_offset=0):

		if not self.logging:
			self.logger = threading.Timer(0, self.__data_logger__, [logfile, frequency, keys, triggers, time_offset])
			self.logging = True
			self.logger.start()
			log.debug("Start logging selected data in file " + logfile + " ...")
		else:
			log.error("The eye-tracker is already in logging mode.")

		return self.logging


	def trigger(self, trigger_key, trigger_value):

		try:
			self.triggers_values[trigger_key] = trigger_value
			log.debug("Trigger received! Key: " + trigger_key + " Value: " + trigger_value)
		except:
			pass


	def stop_logging(self):

		if self.logging:
			self.logging = False
			self.logger.join()
			log.debug("Stop logging!")
		else:
			log.error("The eye-tracker is not in logging mode.")

		return not self.logging

	def close(self):

		"""Neatly close connection to tracker

		arguments
		None

		returns
		None

		"""

		if self.logging:
			self.stop_logging()

		if self.tobiiglasses.is_streaming():
			self.stop_capturing()


	def connected(self):

		"""Checks if the tracker is connected

		arguments
		None

		returns
		connected	--	True if connection is established, False if not

		"""

		res = self.tobiiglasses.wait_until_status_is_ok()

		return res


	def drift_correction(self, pos=None, fix_triggered=False):

		"""Performs a drift check

		arguments
		None

		keyword arguments
		pos			-- (x, y) position of the fixation dot or None for
					   a central fixation (default = None)
		fix_triggered	-- Boolean indicating if drift check should be
					   performed based on gaze position (fix_triggered
					   = True) or on spacepress (fix_triggered =
					   False) (default = False)

		returns
		checked		-- Boolaan indicating if drift check is ok (True)
					   or not (False); or calls self.calibrate if 'q'
					   or 'escape' is pressed


		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def fix_triggered_drift_correction(self, pos=None, min_samples=10, max_dev=60, reset_threshold=30):

		"""Performs a fixation triggered drift correction by collecting
		a number of samples and calculating the average distance from the
		fixation position

		arguments
		None

		keyword arguments
		pos			-- (x, y) position of the fixation dot or None for
					   a central fixation (default = None)
		min_samples		-- minimal amount of samples after which an
					   average deviation is calculated (default = 10)
		max_dev		-- maximal deviation from fixation in pixels
					   (default = 60)
		reset_threshold	-- if the horizontal or vertical distance in
					   pixels between two consecutive samples is
					   larger than this threshold, the sample
					   collection is reset (default = 30)

		returns
		checked		-- Boolaan indicating if drift check is ok (True)
					   or not (False); or calls self.calibrate if 'q'
					   or 'escape' is pressed

		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def get_eyetracker_clock_async(self):

		"""Retrieve difference between tracker time and experiment time

		arguments
		None

		keyword arguments
		None

		returns
		timediff	--	tracker time minus experiment time


		return self.controller.syncmanager.convert_from_local_to_remote(self.controller.clock.get_time()) - clock.get_time()
		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def log(self, msg):

		"""Writes a message to the log file

		arguments
		msg		-- a string to include in the log file

		returns
		Nothing	-- uses native log function of iViewX to include a line
				   in the log file

		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def log_var(self, var, val):

		"""Writes a variable to the log file

		arguments
		var		-- variable name
		val		-- variable value

		returns
		Nothing	-- uses native log function of iViewX to include a line
				   in the log file in a "var NAME VALUE" layout

		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def prepare_backdrop(self):

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def prepare_drift_correction(self, pos):

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")

	
	def eye_position(self):

		"""Returns the 3D positional data for the eye(s)
		
		Whether data for both eyes or just one should be returned is
		determined by the chosen method to do fixation filtering. With
		method 'binocular', an average of the data is returned. With 
		method 'left' or 'right' the data for the left or right eye
		is returned respectively.

		arguments
		None

		returns
		eye_position	-- A dictionary with the 3D positon of the eye(s)
						and the accompanied timestamp and event id gidx

		"""
		if self.tobiiglasses.is_streaming():
			if self.eye_used == 0:
				data = self.get_lefteyedata
				try:
					return data['pc']
				except IndexError:
					log.error("No eye position data available")
					data_dict = {'pc': [-1,-1,-1], 'ts': -1, 'gidx': -1}
					return data_dict

			elif self.eye_used == 1:
				data = self.get_righteyedata
				try:
					return data['pc']
				except IndexError:
					log.error("No eye position data available")
					data_dict = {'pc': [-1,-1,-1], 'ts': -1, 'gidx': -1}
					return data_dict

			elif self.eye_used == 2:
				data_left = self.get_lefteyedata
				data_right = self.get_righteyedata
				try:
					if data_left['pc']['gidx'] != data_right['pc']['gidx']:
						# we got eye positions for different events
						data_dict = {'pc': [-1,-1,-1], 'ts': -1, 'gidx': -1}
						return data_dict

					# data okay, continue
					eye_position = np.average([data_left['pc']['pc'], \
											data_right['pc']['pc']], axis=0)
					ts_avg = (data_left['pc']['ts'] + data_right['pc']['ts'])/2
					data_dict = {'pc': list(eye_position), 
								'ts': ts_avg,
								'gidx': data_left['pc']['gidx']}
					return data_dict
				except IndexError:
					log.error("No eye position data available")
					data_dict = {'pc': [-1,-1,-1], 
								'ts': -1, 
								'gidx': -1}
					return data_dict

		else:
			log.error("The eye-tracker is not in capturing mode.")


	def pupil_size(self):

		"""Returns newest available pupil diameter for left and right eye

		arguments
		None

		returns
		pupilsize	--	a dictionary with left and right eye pupil diameter
						accompanied with the timestamp and event id gidx


		"""

		if self.tobiiglasses.is_streaming():
			ldata = self.get_lefteyedata()
			rdata = self.get_righteyedata()
			try:
				if ldata['pd']['gidx'] == rdata['pd']['gidx']:
					ts_avg = (ldata['pd']['ts'] + rdata['pd']['ts']) / 2

					data_dict = {'left': ldata['pd']['pd'], 
								'right': rdata['pd']['pd'],
								'ts': ts_avg,
								'gidx': ldata['pd']['gidx']}
			except IndexError:
				log.error("No gaze 3d position data available.")

				data_dict = {'left': -1, 
							'right': -1,
							'ts': -1,
							'gidx': -1}
			finally:
				return data_dict
		else:
			log.error("The eye-tracker is not in capturing mode.")


	def sample(self):

		"""Returns newest available gaze position

		arguments
		None

		returns
		sample	-- a dictionary with the gaze position accompanied with the
				   timestamp and the event id gidx

		"""

		if self.tobiiglasses.is_streaming():
			data = self.tobiiglasses.get_gp()
			try:
				data_dict = {'gp': data['gp'], 
							'ts': data['ts'],
							'gidx': data['gidx']}
			except IndexError:
				log.error("No gaze position data available.")
				data_dict = {'gp': [-1,-1], 
							'ts': -1,
							'gidx': -1}
			finally:
				return data_dict
		else:
			log.error("The eye-tracker is not in capturing mode.")


	def sample3D(self):

		"""Returns newest available gaze 3D position 

		arguments
		None

		returns
		sample	-- a dictionary with gp3 data accompanied with the
				timestamp and event id gidx

		"""

		if self.tobiiglasses.is_streaming():
			data = self.tobiiglasses.get_gp3()
			try:
				data_dict = {'gp3': data['gp3'], 
							'ts': data['ts'],
							'gidx': data['gidx']}
			except IndexError:
				log.error("No gaze 3d position data available.")
				data_dict = {'gp3': [-1,-1,-1], 
							'ts': -1,
							'gidx': -1}
			finally:
				return data_dict
		else:
			log.error("The eye-tracker is not in capturing mode.")
		


	def send_command(self, cmd):

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def set_backdrop(self):

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def set_eye_used(self):

		"""Logs the eye_used variable, based on which eye was specified
		(if both eyes are being tracked, the left eye is used)

		arguments
		None

		returns
		Nothing	-- logs which eye is used by calling self.log_var, e.g.
				   self.log_var("eye_used", "right")

		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def start_recording(self):

		"""Starts recording eye position

		arguments
		recording_id

		returns
		None		-- sets self.recording to True when recording is
				   successfully started
		"""

		if self.current_recording_id is None:

			self.current_recording_id = self.tobiiglasses.create_recording(self.current_participant_id)
			try:
				self.tobiiglasses.start_recording(self.current_recording_id)
				log.debug("Recording " + self.current_recording_id + " started!")
			except:
				raise Exception("Error in libtobiiproglasses.TobiiProGlassesController.start_recording: failed to start recording")
		else:
			log.error("The Tobii Pro Glasses is already recording!")


	def status_msg(self, msg):

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")


	def stop_recording(self):

		"""Stop recording eye position

		arguments
		None

		returns
		Nothing	-- sets self.recording to False when recording is
				   successfully started

		"""

		if self.current_recording_id is None:
			log.error("There is no recordings started!")

		else:
			self.tobiiglasses.stop_recording(self.current_recording_id)
			res = self.tobiiglasses.wait_until_recording_is_done(self.current_recording_id)					
			self.current_recording_id = None


	def set_detection_type(self, eventdetection):

		"""Set the event detection type to either PyGaze algorithms, or
		native algorithms as provided by the manufacturer (only if
		available: detection type will default to PyGaze if no native
		functions are available)

		arguments
		eventdetection	--	a string indicating which detection type
						should be employed: either 'pygaze' for
						PyGaze event detection algorithms or
						'native' for manufacturers algorithms (only
						if available; will default to 'pygaze' if no
						native event detection is available)
		returns		--	detection type for saccades, fixations and
						blinks in a tuple, e.g.
						('pygaze','native','native') when 'native'
						was passed, but native detection was not
						available for saccade detection


		"""
		
		# warn if detection is set to native
		if eventdetection == 'native':
			print("WARNING! 'native' event detection has been selected, \
				but Tobii does not provide detection algorithms; PyGaze \
				algorithm will be used instead")
		
		# set event detection methods to PyGaze
		self.eventdetection = 'pygaze'
		
		return (self.eventdetection,self.eventdetection,self.eventdetection)


	def wait_for_event(self, event):

		"""Waits for event

		arguments
		event		-- an integer event code, one of the following:
					3 = STARTBLINK
					4 = ENDBLINK
					5 = STARTSACC
					6 = ENDSACC
					7 = STARTFIX
					8 = ENDFIX

		returns
		outcome	-- a self.wait_for_* method is called, depending on the
				   specified event; the return values of corresponding
				   method are returned
		"""

		if event == 3:
			outcome = self.wait_for_blink_start()
		elif event == 4:
			outcome = self.wait_for_blink_end()
		elif event == 5:
			outcome = self.wait_for_saccade_start()
		elif event == 6:
			outcome = self.wait_for_saccade_end()
		elif event == 7:
			outcome = self.wait_for_fixation_start()
		elif event == 8:
			outcome = self.wait_for_fixation_end()
		else:
			raise Exception("Error in libtobii.TobiiTracker.wait_for_event: eventcode %s is not supported" % event)

		return outcome


	def wait_for_blink_end(self):

		"""Waits for a blink end and returns the blink ending time

		arguments
		None

		returns
		timestamp		--	blink ending time in milliseconds, as
						measured from experiment begin time
		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")

	def wait_for_blink_start(self):

		"""Waits for a blink start and returns the blink starting time

		arguments
		None

		returns
		timestamp		--	blink starting time in milliseconds, as
						measured from experiment begin time

		"""

		"""Not supported for TobiiProGlassesTracker (yet)"""

		print("function not supported yet")

	def wait_for_fixation_start(self, experimental=False):

		"""Returns starting time and position when a fixation is started.


		Function assumes a 'fixation' has started when gaze position
		remains reasonably stable (i.e. when most deviant samples are
		within self.pxfixtresh) for five samples in a row (self.pxfixtresh
		is created in self.calibration, based on self.fixtresh, a property
		defined in self.__init__)

		If running the experimental version, the function will calculate the
		angular velocity between the first and last samples in a 'window' of
		length num_fixation_samples. The middle value in the sample window is
		the sample that we record as a fixation point. Should the velocity be
		less than velocity_threshold, the middle point will be classified as
		part of a fixation.
		
		arguments
		experimental	-- Boolean specifying if experimental variant of
						   fixation detection should be used in place of
						   Pygaze version.
		
		returns
		data_dict		-- A dictionary with the starting gaze position of the
						   fixation and the timestamp. Experimental variant adds
						   the gaze 3d position to the dictionary.
		"""
		
		# # # # #
		# Tobii method

		if self.eventdetection == 'native':
			
			# print warning, since Tobii does not have a fixation start
			# detection built into their API (only ending)
			
			print("WARNING! 'native' event detection has been selected, \
				but Tobii does not offer fixation detection; other \
				algorithm will be used")
			
		# Run pygaze fixation detection (taken from tobiilegacy)
		if not experimental:	
			# # # # #
			# PyGaze method
		
			# function assumes a 'fixation' has started when gaze position
			# remains reasonably stable for self.fixtimetresh
		
			# get starting position
			spos = self.sample()
			while not self.is_valid_sample(spos, 'gp'):
				spos = self.sample()
		
			# get starting time
			t0 = clock.get_time()

			# wait for reasonably stable position
			moving = True
			while moving:
				# get new sample
				npos = self.sample()
				# check if sample is valid
				if self.is_valid_sample(npos, 'gp'):
					# check if new sample is too far from starting position
					if (npos['gp'][0]-spos['gp'][0])**2 + \
						(npos['gp'][1]-spos['gp'][1])**2 > \
						self.pxfixtresh**2:
						# if not, reset starting position and time
						spos = copy.copy(npos)
						t0 = clock.get_time()
					# if new sample is close to starting sample
					else:
						# get timestamp
						t1 = clock.get_time()
						# check if fixation time threshold has been surpassed
						if t1 - t0 >= self.fixtimetresh:
							# return time and starting position
							return {'ts': t0, 'spos': spos}
		# Run experimental fixation detection
		else:
			# Loop until fixation found
			while True:
				if self.init_run:
					# Loop until we got a full window of valid samples.
					while len(self.gaze_samples) < 3 and \
						len(self.gp3_samples) < 3 and \ 
						len(self.eye_samples) < 3:

						# If first time running, get inital samples to fill the
						# window.
						self.init_run = False
						gaze_pos = self.sample()
						spos = self.sample3D()
						eye_position = self.eye_position()

						while not self.is_valid_sample(gaze_pos, 'gp') and \
							not self.is_valid_sample(spos, 'gp3') and\
							not self.is_valid_sample(eye_position, 'pc'):
							# Retry fetching valid samples
							gaze_pos = self.sample()
							spos = self.sample3D()
							eye_position = self.eye_position()

					
						self.gaze_samples.append(gaze_pos)
						self.gp3_samples.append(spos)
						self.eye_samples.append(eye_position)
					
				else:
					# remove oldest sample
					self.gaze_samples = self.gaze_samples[1:]
					self.gp3_samples = self.gp3_samples[1:]
					self.eye_samples = self.eye_samples[1:]

					# Fetch new samples
					gaze_pos = self.sample()
					spos = self.sample3D()
					eye_position = self.eye_position()

					while not self.is_valid_sample(gaze_pos, 'gp') and \
						not self.is_valid_sample(spos, 'gp3') and \
						not self.is_valid_sample(eye_position, 'pc'):
						# Retry fetching valid samples
						gaze_pos = self.sample()
						spos = self.sample3D()
						eye_position = self.eye_position()


					self.gaze_samples.append(gaze_pos)
					self.gp3_samples.append(spos)
					self.eye_samples.append(eye_position)
			
				# make sure that all samples are for the same event
				if is_same_event(self.gaze_samples, 
								self.gp3_samples, 
								self.eye_samples):
					# TODO: Use mems data to correct the angle that the eye(s)
					# have actually moved before calculating the angular 
					# velocity
					ang_vel = self.calculate_angular_velocity(self.gp3_samples, 
															self.eye_samples)
					if ang_vel < self.velocity_threshold:
						# Take the median value of window values. Smooths out the
						# gaze position
						gps_x = [sample['gp'][0] for sample in self.gaze_samples]
						gps_y = [sample['gp'][1] for sample in self.gaze_samples]
						gp3s_x = [sample['gp3'][0] for sample in self.gp3_samples]
						gp3s_y = [sample['gp3'][1] for sample in self.gp3_samples]
						gp_median = [np.median(gps_x), np.median(gps_y)]
						gp3_median = [np.median(gp3s_x), np.median(gp3s_y)]

						return {'ts': self.gaze_samples[1]['ts'],
								'gaze_pos': gp_median, 
								'gp3': gp3_median}

				

	def wait_for_fixation_end(self, experimental=False):

		"""Returns time and gaze position when a fixation has ended;
		function assumes that a 'fixation' has ended when a deviation of
		more than self.pxfixtresh from the initial fixation position has
		been detected (self.pxfixtresh is created in self.calibration,
		based on self.fixtresh, a property defined in self.__init__)

		arguments
		experimental	-- Boolean specifying if experimental variant of
						   fixation detection should be used in place of
						   Pygaze version.

		returns
		data_dict		-- a dictionary with the fixation end time accompanied
						   by the gaze position. With experimental=True, the gp3
						   data is added to the dictionary as well.


		"""

		# # # # #
		# Tobii method

		if self.eventdetection == 'native':
			
			# print warning, since Tobii does not have a fixation detection
			# built into their API
			
			print("WARNING! 'native' event detection has been selected, \
				but Tobii does not offer fixation detection; other algorithm \
				will be used")

		# Run pygaze fixation detection (taken from tobiilegacy)
		if not experimental:
			# # # # #
			# PyGaze method
		
			# function assumes that a 'fixation' has ended when a deviation of more than fixtresh
			# from the initial 'fixation' position has been detected
		
			# get starting time and position
			data = self.wait_for_fixation_start()
			stime = data['ts']
			spos = data['spos']

			# loop until fixation has ended
			while True:
				# get new sample
				npos = self.sample() # get newest sample
				# check if sample is valid
				if self.is_valid_sample(npos, 'gp'):
					# check if sample deviates to much from starting position
					if (npos['gp'][0]-spos['gp'][0])**2 + \
						(npos['gp'][1]-spos['gp'][1])**2 > \
						self.pxfixtresh**2: # Pythagoras
						# break loop if deviation is too high
						break

			return {'fixation_time': clock.get_time(), 
					'gaze_pos': spos}

		# Run experimental fixation detection
		else:
			data = self.wait_for_fixation_start(experimental=True)
			stime = data['ts']
			gaze_pos = data['gaze_pos']
			gp3_pos = data['gp3']
			
			# loop until fixation has ended
			while True:
				# remove oldest sample
				self.gaze_samples = self.gaze_samples[1:]
				self.gp3_samples = self.gp3_samples[1:]
				self.eye_samples = self.eye_samples[1:]

				# Fetch new samples
				gaze_pos = self.sample()
				spos = self.sample3D()
				eye_position = self.eye_position()

				while not self.is_valid_sample(gaze_pos, 'gp') and \
					not self.is_valid_sample(spos, 'gp3') and \
					not self.is_valid_sample(eye_position, 'pc'):
					# Retry fetching valid samples
					gaze_pos = self.sample()
					spos = self.sample3D()
					eye_position = self.eye_position()


				self.gaze_samples.append(gaze_pos)
				self.gp3_samples.append(spos)
				self.eye_samples.append(eye_position)
			
				# make sure that all samples are for the same event
				if is_same_event(self.gaze_samples, 
								self.gp3_samples, 
								self.eye_samples):
					# TODO: Use mems data to correct the angle that the eye(s)
					# have actually moved before calculating the angular 
					# velocity
					ang_vel = self.calculate_angular_velocity(self.gp3_samples, 
															self.eye_samples)
					fixation_time = self.gaze_samples[1]['ts'] - stime
					if ang_vel > self.velocity_threshold and \
						fixation_time > self.fixtimetresh: 

						return {'fixation_time': fixation_time,
								'gaze_pos': gaze_pos, 
								'gp3': gp3_pos}



	def wait_for_saccade_start(self):

		"""Returns starting time and starting position when a saccade is
		started; based on Dalmaijer et al. (2013) online saccade detection
		algorithm

		arguments
		None

		returns
		endtime, startpos	-- endtime in milliseconds (from expbegintime);
							   startpos is an (x,y) gaze position tuple

		"""

		# # # # #
		# Tobii method

		if self.eventdetection == 'native':
			
			# print warning, since Tobii does not have a blink detection
			# built into their API
			
			print("WARNING! 'native' event detection has been selected, \
				but Tobii does not offer saccade detection; PyGaze \
				algorithm will be used")

		# # # # #
		# PyGaze method

		# get starting position (no blinks)
			newpos = self.sample()
			while not self.is_valid_sample(newpos, 'gp'):
				newpos = self.sample()
			# get starting time, position, intersampledistance, and velocity
			t0 = clock.get_time()
			prevpos = newpos
			s = 0
			v0 = 0

			# get samples
			saccadic = False
			while not saccadic:
				# get new sample
				newpos = self.sample()
				t1 = clock.get_time()
				if self.is_valid_sample(newpos, 'gp') and \
					newpos['gp'] != prevpos['gp']:
					# check if distance is larger than precision error
					sx = newpos['gp'][0] - prevpos['gp'][0]
					sy = newpos['gp'][1] - prevpos['gp'][1]
					# weigthed distance: (sx/tx)**2 + (sy/ty)**2 > 1 means
					# movement larger than RMS noise
					if (sx/self.pxdsttresh[0])**2 + (sy/self.pxdsttresh[1])**2 \
						> self.weightdist:
						# calculate distance
						# intersampledistance = speed in pixels/ms
						s = ((sx)**2 + (sy)**2)**0.5
						# calculate velocity
						v1 = s / (t1-t0)
						# calculate acceleration
						a = (v1-v0) / (t1-t0) # acceleration in pixels/ms**2
						# check if either velocity or acceleration are above
						# threshold values
						if v1 > self.pxspdtresh or a > self.pxacctresh:
							saccadic = True
							spos = prevpos['gp'][:]
							stime = clock.get_time()
						# update previous values
						t0 = copy.copy(t1)
						v0 = copy.copy(v1)
					# udate previous sample
					prevpos = newpos
			return stime, spos

	def wait_for_saccade_end(self):

		"""Returns ending time, starting and end position when a saccade is
		ended; based on Dalmaijer et al. (2013) online saccade detection
		algorithm

		arguments
		None

		returns
		endtime, startpos, endpos	-- endtime in milliseconds (from
							   expbegintime); startpos and endpos
							   are (x,y) gaze position tuples

		"""

		# # # # #
		# Tobii method

		if self.eventdetection == 'native':
			
			# print warning, since Tobii does not have a blink detection
			# built into their API
			
			print("WARNING! 'native' event detection has been selected, \
				but Tobii does not offer saccade detection; PyGaze \
				algorithm will be used")

		# # # # #
		# PyGaze method
		
		# get starting position (no blinks)
		t0, spos = self.wait_for_saccade_start()
		# get valid sample
		prevpos = self.sample()
		while not self.is_valid_sample(prevpos, 'gp'):
			prevpos = self.sample()
		# get starting time, intersample distance, and velocity
		t1 = clock.get_time()
		# = intersample distance = speed in px/sample
		s = ((prevpos['gp'][0] - spos['gp'][0])**2 + \
			(prevpos['gp'][1] - spos['gp'][1])**2)**0.5 
		v0 = s / (t1-t0)

		# run until velocity and acceleration go below threshold
		saccadic = True
		while saccadic:
			# get new sample
			newpos = self.sample()
			t1 = clock.get_time()
			if self.is_valid_sample(newpos,'gp') and \
				newpos['gp'] != prevpos['gp']:
				# calculate distance
				# speed in pixels/sample
				s = ((newpos['gp'][0]-prevpos['gp'][0])**2 + \
					(newpos['gp'][1]-prevpos['gp'][1])**2)**0.5 
				# calculate velocity
				v1 = s / (t1-t0)
				# calculate acceleration
				# acceleration in pixels/sample**2 
				a = (v1-v0) / (t1-t0) 
				# check if velocity and acceleration are below threshold
				if v1 < self.pxspdtresh and (a > -1*self.pxacctresh and a < 0):
					saccadic = False
					epos = newpos['gp'][:]
					etime = clock.get_time()
				# update previous values
				t0 = copy.copy(t1)
				v0 = copy.copy(v1)
			# udate previous sample
			prevpos = newpos

		return etime, spos, epos

	def is_valid_sample(self, sample, sample_type):

		"""Checks if the sample provided is valid, based on Tobii specific
		criteria (for internal use)

		arguments
		sample		--	A sample of sample type to be checked

		sample_type	-- String telling the type of the supplied sample

		returns
		valid		--	a Boolean: True on a valid sample, False on
					    an invalid sample

		"""
		if sample_type == 'gp3':
			if sample['gp3'] == [-1,-1,-1]:
				return False
		elif sample_type == 'gp':
			if sample['gp'] == [-1,-1]:
				return False
		elif sample_type == 'pc':
			if sample['pc'] == [-1,-1,-1]:
				return False
		else:
			# supplied sample type not supported. log error.
			log.error("The supplied sample type {} is not supported.".format(sample_type))

		# in any other case, the sample is valid
		return True


	def is_same_event(self, gaze_samples, gp3_samples, eye_samples):

		"""Checks that samples from livedata is from the same gaze event.

		Tobii glasses has a key value gidx, indicating which gaze event a
		sample is part of. It is used to check that each sample from gaze 
		position and eye position are from the same event. Gidx should be
		the equal at the same position in the sample list.

		arguments
		gaze_samples	-- gaze position samples
		gp3_samples		-- gaze 3d position samples
		eye_samples		-- eye samples

		returns
		valid		-- Boolean which is True if all samples share the same gidx
					   and False if they do not.
		
		"""
		
		valid = all(gaze['gidx'] == gp3['gidx'] == eye['gidx'] for \
				gaze, gp3, eye in zip(gaze_samples, gp3_samples, eye_samples))
		return valid

	def get_data(self):

		return self.tobiiglasses.data

	def get_mems(self):

		return self.tobiiglasses.data['mems']

	def get_gp(self):

		return self.tobiiglasses.data['gp']

	def get_gp3(self):

		return self.tobiiglasses.data['gp3']

	def get_lefteyedata(self):

		return self.tobiiglasses.data['left_eye']

	def get_righteyedata(self):

		return self.tobiiglasses.data['right_eye']
