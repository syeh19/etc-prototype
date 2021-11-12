from astropy import units as u
from Instrument import instrument
from Source import source
from Atmosphere import atmosphere
import yaml
from numpy import pi, linspace, zeros, array, arccos, sqrt, NaN
from warnings import warn

class exposure_time_calculator:

    global _CONFIG_FILEPATH; _CONFIG_FILEPATH = './config.yaml'

    def _mount_config(self, config_path):
        # From https://www.geeksforgeeks.org/convert-nested-python-dictionary-to-object/
        def _dict2obj(d):
            # If list, recursively unpack
            if isinstance(d, list):
                d = [_dict2obj(x) for x in d]
            # If not list or dictionary, return object
            if not isinstance(d, dict):
                return d
            # Otherwise, create dummy object
            class Foo:
                pass
            obj = Foo()
            # Loop over dictionary items and add to object
            for x in d:
                obj.__dict__[x] = _dict2obj(d[x])
            return obj
        # Open config file, convert & mount to self
        config = yaml.safe_load(open(config_path))
        config = _dict2obj(config)
        self.__dict__.update({'config': config})


    def _validate_config(self):
        pass  # TODO


    def _calculate(self):

        slit_size = self.instrument.slit_width * self.instrument.slit_length
        slit_size_pixels = slit_size / self.instrument.pixel_size
        # Area of gaussian distributed point source minus area of segments
        area_occluded = (self.atmosphere.seeing**2 * arccos(self.instrument.slit_width / self.atmosphere.seeing)/u.rad - self.instrument.slit_width * sqrt(self.atmosphere.seeing**2 - self.instrument.slit_width**2))/2 if self.atmosphere.seeing > self.instrument.slit_width else 0
        source_size = (pi * (self.atmosphere.seeing/2)**2) - area_occluded
        source_slit_ratio = source_size / (pi * (self.atmosphere.seeing/2)**2)

        # TODO -- coadds, reads, all the juicy details
        self.source_flux = self.source.get_flux(self.wavelengths) * self.atmosphere.get_transmission(self.wavelengths)
        source_rate = self.source_flux * self.instrument.get_throughput(self.wavelengths) * self.binning[1]  # Binning in the spectral direction
        source_rate *= self.telescope_area * source_slit_ratio * self.wavelengths / self.instrument.spectral_resolution * self.reads
        self.efficiency = (self.atmosphere.get_transmission(self.wavelengths) * self.instrument.get_throughput(self.wavelengths)).value  # Save efficiency as dimensionless, not e-/ph


        # Also, why isn't the throughput included in Sherry's code?
        background_rate = self.atmosphere.get_emission(self.wavelengths) * self.instrument.get_throughput(self.wavelengths)
        background_rate *= self.telescope_area * slit_size * self.wavelengths * self.reads

        # Divide reads by 2 because read noise is per CDS (2 reads)
        read_noise = self.instrument.get_read_noise()**2 * self.reads/2 * slit_size_pixels / self.binning[0]  # Binning in the spatial direction
        dark_current_rate = self.instrument.get_dark_current() * slit_size_pixels
        
        if self.target == 'signal_noise_ratio':
            if len(self.exposure) == 0:
                self.exposure = [u.Quantity(x) for x in self.config.defaults.exposure] * u.s
                warn('In ETC -- exposure is not defined, defaulting to '+str(self.exposure), RuntimeWarning)

            self.source_count = [source_rate * exp for exp in self.exposure] * u.electron
            self.background_count = [background_rate * exp for exp in self.exposure] * u.electron
            self.dark_current_count = [dark_current_rate * exp for exp in self.exposure] * u.electron
            self.read_noise_count = ([read_noise] * len(self.exposure)) * u.electron
            noise_count = [self.source_count[exp] + self.background_count[exp] + self.dark_current_count[exp] + self.read_noise_count[exp] for exp in range(len(self.exposure))] # Total count in e- for whole slit and exposure
            self.signal_noise_ratio = [(self.source_count[exp] * noise_count[exp] ** (-1/2) * self.dithers ** (1/2)).value for exp in range(len(self.exposure))] * u.dimensionless_unscaled # Remove the sqrt(e-) unit because it's nonphysical

        elif self.target == 'exposure':
            if len(self.signal_noise_ratio) == 0:
                self.signal_noise_ratio = [u.Quantity(x) for x in self.config.defaults.signal_noise_ratio] * u.dimensionless_unscaled
                warn('In ETC -- signal_noise_ratio is not defined, defaulting to '+str(self.signal_noise_ratio), RuntimeWarning)

            self.exposure = zeros([len(self.signal_noise_ratio), len(self.wavelengths)])
            for idx, snr in enumerate(self.signal_noise_ratio.value * u.electron**(1/2)):
                # Adding 0j to avoid generating RuntimeWarning for sqrt(-1)
                a = self.dithers * source_rate**2 + 0j
                b = - snr**2 * (background_rate + dark_current_rate + source_rate) + 0j
                c = [(read_noise * snr**2).to(u.electron**2).value] * len(self.wavelengths) * u.electron**2 + 0j
                exposure = ( -b + (b**2 - 4*a*c)**(1/2) ) / (2 * a)
                #exposure_neg = ( -b - (b**2 - 4*a*c)**(1/2) ) / (2 * a)
                # This statement is broken, iter() needs to be moved outside because it reinitializes every time
                #exposure = [next(iter(exposure_pos)) if check else next(iter(exposure_neg)) for check in (exposure_pos.real >= 0) & (exposure_pos.imag == 0)] * u.s
                if ((exposure.real < 0) | (exposure.imag != 0)).any():
                    exposure[(exposure.real < 0) | (exposure.imag != 0)] = NaN
                    warn('In ETC -- Some/all nonexistent solutions found for S/N = '+str(snr.value)+', returning exposure = NaN', RuntimeWarning)
                self.exposure[idx, :] = exposure.real.to(u.s)
            self.exposure = u.Quantity(self.exposure, u.s)
            # Calculate and save counts
            self.source_count = [source_rate * exp for exp in self.exposure] * u.electron
            self.background_count = [background_rate * exp for exp in self.exposure] * u.electron
            self.dark_current_count = [dark_current_rate * exp for exp in self.exposure] * u.electron
            self.read_noise_count = ([[read_noise.to(u.electron).value] * len(self.wavelengths)] * len(self.exposure)) * u.electron
            
        else:
            # Check that exposure and S/N have not both been provided
            raise ValueError('ERROR: In ETC -- target must be set to "exposure" or "signal_noise_ratio"')


    def __init__(self):
        # Set default values based on config file
        self._mount_config(_CONFIG_FILEPATH)
        self._validate_config()

        # Initialize objects
        self.instrument = instrument(self.config.defaults.instrument)
        self.atmosphere = atmosphere()
        self.source = source()

        # Initialize values
        self.telescope_area = u.Quantity(self.config.telescope_area)
        self.exposure = [u.Quantity(x) for x in self.config.defaults.exposure] * u.s
        self.signal_noise_ratio = [u.Quantity(x) for x in self.config.defaults.signal_noise_ratio] * u.dimensionless_unscaled
        self.dithers = u.Quantity(self.config.defaults.dithers)
        self.reads = u.Quantity(self.config.defaults.reads)
        self.repeats = u.Quantity(self.config.defaults.repeats)
        self.coadds = u.Quantity(self.config.defaults.coadds)
        self.target = self.config.defaults.target
        self.binning = u.Quantity(self.config.defaults.binning)
        # Calculate default wavelengths array from min, max of instrument and atmosphere
        min_wavelength = max(self.atmosphere._wavelength_index[0], self.instrument.min_wavelength)
        max_wavelength = min(self.atmosphere._wavelength_index[-1], self.instrument.max_wavelength)
        self.wavelengths = linspace(min_wavelength, max_wavelength, self.config.defaults.default_wavelengths_number)

        self._calculate()


    def set_parameter(self, name, value):
        # TODO -- validation
        if name.startswith('instrument.'):
            self._set_instrument_parameter('.'.join(name.split('.')[1:]), value)
        elif name.startswith('source.'):
            self._set_source_parameter('.'.join(name.split('.')[1:]), value)
        elif name.startswith('atmosphere.'):
            self._set_atmosphere_parameter('.'.join(name.split('.')[1:]), value)
        else:
            if name == 'exposure':
                self.target = 'signal_noise_ratio'
                self.exposure = [u.Quantity(x).to(u.s) for x in value] * u.s
            elif name == 'signal_noise_ratio':
                self.target = 'exposure'
                self.signal_noise_ratio = [u.Quantity(x) for x in value] * u.dimensionless_unscaled
            else:
                vars(self)[name] = u.Quantity(value)
        self._calculate()

    
    def _set_source_parameter(self, name, value):
        # TODO -- input validation
        if name == 'type':
            self.source.set_type(value)
        elif name == 'wavelength_band':
            self.source.wavelength_band = str(value)
        elif name == 'brightness':
            self.source.set_brightness(value)
        elif name == 'temperature':
            self.source.temperature = u.Quantity(value).to(u.K, equivalencies=u.temperature())
        else:
            vars(self.source)[name] = u.Quantity(value)
            self._calculate()

    def _set_atmosphere_parameter(self, name, value):
        # TODO -- input validation
        vars(self.atmosphere)[name] = u.Quantity(value)
        self._calculate()
    
    def _set_instrument_parameter(self, name, value):
        # TODO -- input validation
        if name == 'name':
            self.instrument.set_name(value)
        else:
            vars(self.instrument)[name] = u.Quantity(value)
        self._calculate()